"""GitHub gateway through the GitHub App (jooservices-aiflow-bot).

The control plane holds the App's private key. Nodes never see it: they get a short-lived installation token
narrowed to one repository (contents and pull requests), and only while their fencing token is valid.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubApp:
    def __init__(self, app_dir: str, org: str, client: httpx.Client | None = None) -> None:
        d = Path(app_dir)
        self.meta = json.loads((d / "app.json").read_text())
        self.key = (d / "app.pem").read_text()
        self.org = org
        self.c = client or httpx.Client(base_url=API, timeout=30, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiflow4"})
        self._inst: int | None = None
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.meta["id"])}, self.key, algorithm="RS256")

    def _req(self, method: str, path: str, token: str, body: Any = None, ok=(200, 201, 202, 204)) -> Any:
        r = self.c.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code not in ok:
            raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def installation_id(self) -> int:
        if self._inst is None:
            self._inst = int(self._req("GET", f"/orgs/{self.org}/installation", self._jwt())["id"])
        return self._inst

    def token(self, repo: str | None = None, permissions: dict[str, str] | None = None) -> tuple[str, datetime]:
        """Installation token, optionally narrowed to one repo and to the given permissions; cached ~50 min."""
        perms = permissions or {"contents": "write", "pull_requests": "write", "metadata": "read"}
        cache_key = f"{repo}|{sorted(perms.items())}"
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and hit[1] - time.time() > 600:
                return hit[0], datetime.fromtimestamp(hit[1]).astimezone()
            body: dict[str, Any] = {"permissions": perms}
            if repo:
                body["repositories"] = [repo.split("/", 1)[-1]]
            data = self._req("POST", f"/app/installations/{self.installation_id()}/access_tokens", self._jwt(), body)
            exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            self._cache[cache_key] = (data["token"], exp.timestamp())
            return data["token"], exp

    def api(self, method: str, path: str, body: Any = None, repo: str | None = None, ok=(200, 201, 202, 204)) -> Any:
        tok, _ = self.token(repo, {"contents": "write", "pull_requests": "write", "metadata": "read",
                                   "checks": "read", "statuses": "read"} if repo else None)
        return self._req(method, path, tok, body, ok)

    # --- pull requests --------------------------------------------------------------------------------------
    def find_pr(self, repo: str, head: str) -> dict | None:
        prs = self.api("GET", f"/repos/{self.org}/{repo}/pulls?state=open&head={self.org}:{head}", repo=repo)
        return prs[0] if prs else None

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool = True) -> dict:
        existing = self.find_pr(repo, head)
        if existing:
            return existing
        return self.api("POST", f"/repos/{self.org}/{repo}/pulls",
                        {"title": title, "head": head, "base": base, "body": body, "draft": draft}, repo=repo)

    def bot_identity(self) -> tuple[str, str]:
        slug = self.meta["slug"]
        user = self._req("GET", f"/users/{slug}%5Bbot%5D", self.token()[0])
        return f"{slug}[bot]", f"{user['id']}+{slug}[bot]@users.noreply.github.com"
