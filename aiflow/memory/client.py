"""The client of the memory service (standard library only). Memory is advice, never a requirement: a search that cannot be answered
returns nothing, and a proposal that cannot be sent is kept in a local spool and sent later."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


class Unavailable(RuntimeError):
    pass


class Memory:
    def __init__(self, url: str, token: str, spool: Path | None = None, timeout: float = 4.0,
                 opener: Callable[..., Any] = urllib.request.urlopen, log: Callable[[str], None] = lambda _m: None) -> None:
        self.url, self.token, self.spool, self.timeout, self.opener, self.log = url.rstrip("/"), token, spool, timeout, opener, log
        self.down_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        """The decoded answer; raises Unavailable when the service cannot be reached, ValueError(message) when it refuses."""
        if not self.enabled:
            raise Unavailable("no memory service configured")
        if time.time() < self.down_until:
            raise Unavailable("the memory service was down a moment ago")
        request = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read()).get("error", "")
            except Exception:
                pass
            if error.code in (503, 502, 504):
                self.down_until = time.time() + 30
                raise Unavailable(detail or f"HTTP {error.code}") from error
            raise ValueError(f"HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            self.down_until = time.time() + 30  # do not make every ticket wait for a dead service
            raise Unavailable(str(error)) from error

    # ---- read (never raises) --------------------------------------------------------------------------------------
    def search(self, query: str, namespace: str = "", project: str = "", repo: str = "", kinds: list[str] | None = None, limit: int = 6,
               touch: bool = True) -> list[dict[str, Any]]:
        try:
            return self.call("POST", "/v1/search", dict(query=query, namespace=namespace, project=project, repo=repo, kinds=kinds, limit=limit, touch=touch))["items"]
        except (Unavailable, ValueError, KeyError) as error:
            self.log(f"memory search skipped: {error}")
            return []

    def get(self, entry_id: str) -> dict[str, Any]:
        return self.call("GET", f"/v1/entries/{entry_id}")

    # ---- write -----------------------------------------------------------------------------------------------------
    def propose(self, entry: dict[str, Any]) -> str | None:
        """Send an entry; when the service is down keep it in the spool. Returns the id, or None when spooled or refused."""
        try:
            return self.call("POST", "/v1/entries", entry)["id"]
        except Unavailable as error:
            self.keep(entry)
            self.log(f"memory entry kept for later: {error}")
        except ValueError as error:
            self.log(f"memory entry refused: {error}")
        return None

    def feedback(self, entry_id: str, jira_key: str, helpful: bool) -> None:
        try:
            self.call("POST", f"/v1/entries/{entry_id}/feedback", dict(jira_key=jira_key, helpful=helpful))
        except (Unavailable, ValueError) as error:
            self.log(f"memory feedback skipped: {error}")

    def keep(self, entry: dict[str, Any]) -> None:
        if self.spool:
            self.spool.parent.mkdir(parents=True, exist_ok=True)
            with self.spool.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")

    def flush(self) -> int:
        """Send the spooled entries; the ones the service refuses are dropped, the ones it cannot take stay. Returns how many were sent."""
        if not self.spool or not self.spool.is_file():
            return 0
        lines = [line for line in self.spool.read_text(encoding="utf-8").splitlines() if line.strip()]
        left, sent = [], 0
        for index, line in enumerate(lines):
            try:
                self.call("POST", "/v1/entries", json.loads(line))
                sent += 1
            except Unavailable:
                left = lines[index:]
                break
            except ValueError:
                continue  # refused for good (a secret, a wrong scope): not worth keeping
        self.spool.write_text("".join(f"{line}\n" for line in left), encoding="utf-8")
        return sent

    def curate(self) -> dict[str, int] | None:
        try:
            return self.call("POST", "/v1/curate", {})
        except (Unavailable, ValueError):
            return None
