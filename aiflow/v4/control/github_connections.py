"""Registry of GitHub connections held by Command Center, with the repository to connection assignments.

The first connection is the legacy single GitHub App in the secret directory (``app.json`` and ``app.pem``); it is
always called ``default``. Further connections live next to it in ``connections/<id>/``: a GitHub App (the same
``app.json`` and ``app.pem`` pair) or a personal or fine-grained access token (``token``). ``assignments.json`` maps
a repository to the connection owners chose for it. Everything is owner-only files; nodes never read this directory.

A connection reaches the repositories of one account or organization. A repository is served by its assigned
connection, or otherwise by the first connection of its owner that lists it; a repository no connection reaches
is refused rather than guessed.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .github import API, GitHubApp, GitHubError
from .github_connect import GitHubSecretStore

DEFAULT_ID = "default"
_ID = re.compile(r"^[a-f0-9]{12}$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REACH_TTL = 30.0


class GitHubTokenApp(GitHubApp):
    """A personal or fine-grained access token behind the GitHubApp call surface, for one account.

    The token cannot be narrowed or shortened, so it never leaves Command Center: nodes get no token for a
    repository on this connection (see ``kind``)."""

    kind = "token"

    def __init__(self, token: str, account: str, account_type: str = "", client: httpx.Client | None = None) -> None:
        self.dir = Path(".")
        self.key = ""
        self.org = account
        self._token = token
        self.meta: dict[str, Any] = {"id": "", "account_login": account, "account_type": account_type}
        self._inst = None
        self._cache = {}
        self._lock = threading.RLock()
        self.c = client or httpx.Client(base_url=API, timeout=30, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiflow4"})

    def _refresh_config(self, force: bool = False) -> None:
        return None

    def token(self, repo: str | None = None, permissions: dict[str, str] | None = None) -> tuple[str, datetime]:
        return self._token, datetime.now(timezone.utc) + timedelta(hours=1)

    def user(self) -> dict[str, Any]:
        return self._req("GET", "/user", self._token)

    def installation_repositories(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        page = 1
        while True:
            rows = self._req("GET", f"/user/repos?per_page=100&page={page}&sort=full_name", self._token) or []
            out.extend({"full_name": str(r.get("full_name") or ""), "name": str(r.get("name") or ""),
                        "default_branch": str(r.get("default_branch") or "")}
                       for r in rows if r.get("full_name")
                       and str((r.get("owner") or {}).get("login") or "").lower() == self.org.lower())
            if len(rows) < 100:
                return out
            page += 1

    def bot_identity(self) -> tuple[str, str]:
        user = self.user()
        return str(user["login"]), f"{user['id']}+{user['login']}@users.noreply.github.com"


def verify_token(token: str, account: str = "", client: httpx.Client | None = None) -> GitHubTokenApp:
    """Ask GitHub who the token belongs to and which repositories of the account it reaches."""
    probe = GitHubTokenApp(token, account or "-", client=client)
    user = probe.user()
    login = str(user.get("login") or "")
    if not login:
        raise GitHubError("GitHub did not identify the token")
    account = account or login
    kind = "User" if account.lower() == login.lower() else "Organization"
    gateway = GitHubTokenApp(token, account, kind, client=probe.c)
    gateway.installation_repositories()
    return gateway


class Connection:
    """One entry of the registry as the API sees it."""

    def __init__(self, id: str, label: str, kind: str, account: str, account_type: str, gateway: Any) -> None:
        self.id, self.label, self.kind = id, label, kind
        self.account, self.account_type, self.gateway = account, account_type, gateway

    def summary(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "kind": self.kind, "account": self.account,
                "account_type": self.account_type}


class GitHubConnections:
    """Filesystem registry of the additional connections and of the repository assignments."""

    def __init__(self, root: str | Path, app_factory: Callable[[str, str], Any],
                 token_factory: Callable[[str, str, str], Any] = GitHubTokenApp) -> None:
        self.root = Path(root)
        self.dir = self.root / "connections"
        self.app_factory = app_factory
        self.token_factory = token_factory
        self._gateways: dict[str, tuple[tuple, Any]] = {}
        self._reach: dict[str, tuple[float, set[str]]] = {}
        self._lock = threading.RLock()
        self._store = GitHubSecretStore(self.root)

    # --- stored records ---------------------------------------------------------------------------------------
    def _path(self, conn_id: str) -> Path:
        if not _ID.fullmatch(conn_id):
            raise ValueError("unknown GitHub connection")
        return self.dir / conn_id

    def meta(self, conn_id: str) -> dict[str, Any] | None:
        try:
            return json.loads((self._path(conn_id) / "connection.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def ids(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        rows = [(m.get("created_at", 0), p.name) for p in self.dir.iterdir()
                if _ID.fullmatch(p.name) and (m := self.meta(p.name))]
        return [name for _, name in sorted(rows)]

    def has_extras(self) -> bool:
        return bool(self.ids())

    def _save(self, conn_id: str, meta: dict[str, Any], secrets_: dict[str, str]) -> None:
        directory = self._path(conn_id)
        for name, value in secrets_.items():
            self._store._write_file(directory / name, value)
        self._store._write_file(directory / "connection.json", json.dumps(meta, separators=(",", ":")))
        with self._lock:
            self._gateways.pop(conn_id, None)
            self._reach.pop(conn_id, None)

    def add_token(self, label: str, token: str, account: str, account_type: str) -> str:
        conn_id = secrets.token_hex(6)
        self._save(conn_id, {"id": conn_id, "label": label, "kind": "token", "account": account,
                             "account_type": account_type, "created_at": time.time()}, {"token": token})
        return conn_id

    def replace_token(self, conn_id: str, token: str, account: str, account_type: str) -> None:
        meta = self.meta(conn_id)
        if not meta or meta.get("kind") != "token":
            raise ValueError("unknown GitHub token connection")
        meta.update({"account": account, "account_type": account_type})
        self._save(conn_id, meta, {"token": token})

    def save_app(self, conn_id: str | None, label: str, app_meta: dict[str, Any], pem: str) -> str:
        """Store an App as a new connection, or replace the key and installation of an existing one."""
        existing = self.meta(conn_id) if conn_id else None
        conn_id = conn_id if existing else secrets.token_hex(6)
        directory = self._path(conn_id)
        from .github_connect import app_metadata
        saved = app_metadata(app_meta)
        meta = {"id": conn_id, "label": label or (existing or {}).get("label") or saved.get("name") or "GitHub App",
                "kind": "app", "account": saved.get("account_login", ""),
                "account_type": saved.get("account_type", ""),
                "created_at": (existing or {}).get("created_at", time.time())}
        self._store._write_file(directory / "app.json", json.dumps(saved, separators=(",", ":")))
        self._save(conn_id, meta, {"app.pem": pem})
        return conn_id

    def remove(self, conn_id: str) -> None:
        shutil.rmtree(self._path(conn_id), ignore_errors=True)
        with self._lock:
            self._gateways.pop(conn_id, None)
            self._reach.pop(conn_id, None)
        assigned = self.assignments()
        kept = {repo: cid for repo, cid in assigned.items() if cid != conn_id}
        if kept != assigned:
            self._write_assignments(kept)

    def gateway(self, conn_id: str) -> Any | None:
        meta = self.meta(conn_id)
        if not meta:
            return None
        directory = self._path(conn_id)
        try:
            stamp = tuple(f.stat().st_mtime_ns for f in sorted(directory.iterdir()))
        except OSError:
            return None
        with self._lock:
            hit = self._gateways.get(conn_id)
            if hit and hit[0] == stamp:
                return hit[1]
            if meta["kind"] == "token":
                gateway = self.token_factory((directory / "token").read_text(encoding="utf-8").strip(),
                                             meta["account"], meta.get("account_type", ""))
            else:
                gateway = self.app_factory(str(directory), meta["account"])
            self._gateways[conn_id] = (stamp, gateway)
            return gateway

    def connections(self) -> list[Connection]:
        out = []
        for conn_id in self.ids():
            meta, gateway = self.meta(conn_id), self.gateway(conn_id)
            if meta and gateway is not None:
                out.append(Connection(conn_id, meta["label"], meta["kind"], meta["account"],
                                      meta.get("account_type", ""), gateway))
        return out

    # --- repository assignments -------------------------------------------------------------------------------
    def assignments(self) -> dict[str, str]:
        try:
            data = json.loads((self.root / "assignments.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _write_assignments(self, data: dict[str, str]) -> None:
        self._store._write_file(self.root / "assignments.json", json.dumps(data, separators=(",", ":")))

    def assign(self, repo: str, conn_id: str | None) -> None:
        data = self.assignments()
        if conn_id:
            data[repo.lower()] = conn_id
        else:
            data.pop(repo.lower(), None)
        self._write_assignments(data)

    # --- reachability -----------------------------------------------------------------------------------------
    def reaches(self, conn_id: str, gateway: Any, repo: str) -> bool:
        """True when the connection lists the repository; a miss is re-read from GitHub before it is final."""
        key = repo.lower()
        with self._lock:
            hit = self._reach.get(conn_id)
            if hit and time.time() - hit[0] < REACH_TTL and key in hit[1]:
                return True
        try:
            names = {str(r.get("full_name") or "").lower() for r in gateway.installation_repositories()}
        except Exception:  # noqa: BLE001 - an unreachable provider means the repository is not served
            return False
        with self._lock:
            self._reach[conn_id] = (time.time(), names)
        return key in names
