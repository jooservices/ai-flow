"""GitHub App onboarding helpers and filesystem-backed private-key storage.

The control plane is the only process allowed to read these files. Temporary setup records and the active App
configuration live in the configured secret directory, never in the registry or control repo.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from ..core import secretbox


_ID = re.compile(r"^[a-f0-9]{64}$")
_META_FIELDS = ("id", "slug", "name", "html_url", "account_login", "account_type", "installation_id",
                "repository_names")


def app_metadata(data: dict[str, Any], *, account_login: str = "", account_type: str = "",
                 installation_id: int | None = None) -> dict[str, Any]:
    """Keep only the identifiers needed by the gateway; never persist client or webhook secrets."""
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    result = {key: str(data[key]) for key in ("id", "slug", "name", "html_url") if data.get(key) is not None}
    result["id"] = str(data.get("id") or "")
    if account_login:
        result["account_login"] = account_login
        result["account_type"] = account_type or str(data.get("account_type") or "")
    elif data.get("account_login"):
        result["account_login"] = str(data["account_login"])
        result["account_type"] = str(data.get("account_type") or "")
    elif owner.get("login"):
        result["account_login"] = str(owner["login"])
        result["account_type"] = str(owner.get("type") or "")
    saved_installation = installation_id if installation_id is not None else data.get("installation_id")
    if saved_installation is not None:
        result["installation_id"] = int(saved_installation)
    if isinstance(data.get("repository_names"), list):
        result["repository_names"] = sorted({str(name) for name in data["repository_names"] if name})
    return {key: result[key] for key in _META_FIELDS if key in result}


class GitHubSecretStore:
    """Store active and short-lived pending GitHub App credentials with owner-only file permissions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.pending = self.root / ".pending"

    @property
    def active_exists(self) -> bool:
        return (self.root / "app.json").is_file() and (self.root / "app.pem").is_file()

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(32)

    def _pending_dir(self, key: str) -> Path:
        if not _ID.fullmatch(key):
            raise ValueError("invalid GitHub setup state")
        return self.pending / key

    def migrate_plaintext(self) -> int:
        """Encrypt every legacy plaintext private key (active, pending and per-connection) and check the rest decrypt.

        Raises (fail closed) without a usable external key, so the API never serves with a plaintext key on disk."""
        changed = 0
        if not self.root.is_dir():
            return 0
        for path in sorted(self.root.rglob("app.pem")):
            raw = path.read_text(encoding="utf-8")
            if secretbox.is_encrypted(raw):
                secretbox.decrypt(raw, context="app.pem")
                continue
            self._write_file(path, raw)
            changed += 1
        return changed

    def _write_file(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if path.name == "app.pem":
            contents = secretbox.seal(contents, "app.pem")  # fails closed if the external key is missing
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            os.chmod(path, 0o600)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def create_pending(self, key: str, record: dict[str, Any], private_key: str = "") -> None:
        self.prune_pending()
        directory = self._pending_dir(key)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        record = dict(record)
        record.setdefault("created_at", int(time.time()))
        self._write_file(directory / "setup.json", json.dumps(record, separators=(",", ":")))
        if private_key:
            self._write_file(directory / "app.pem", private_key)
            self._write_file(directory / "app.json", json.dumps(app_metadata(record.get("app") or {}),
                                                                  separators=(",", ":")))

    def prune_pending(self) -> None:
        if not self.pending.is_dir():
            return
        cutoff = int(time.time()) - 3600
        for directory in self.pending.iterdir():
            if not directory.is_dir() or not _ID.fullmatch(directory.name):
                continue
            try:
                record = json.loads((directory / "setup.json").read_text(encoding="utf-8"))
                if int(record.get("created_at", 0)) < cutoff:
                    shutil.rmtree(directory, ignore_errors=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                shutil.rmtree(directory, ignore_errors=True)

    def read_pending(self, key: str) -> tuple[dict[str, Any], dict[str, Any], str]:
        directory = self._pending_dir(key)
        record = json.loads((directory / "setup.json").read_text(encoding="utf-8"))
        if int(record.get("created_at", 0)) < int(time.time()) - 3600:
            self.delete_pending(key)
            raise ValueError("GitHub setup expired")
        meta_path, key_path = directory / "app.json", directory / "app.pem"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        pem = ""
        if key_path.exists():
            raw = key_path.read_text(encoding="utf-8")
            pem = secretbox.decrypt(raw, context="app.pem")
            if not secretbox.is_encrypted(raw):
                self._write_file(key_path, pem)
        return record, meta, pem

    def update_pending(self, key: str, record: dict[str, Any], meta: dict[str, Any], private_key: str) -> None:
        directory = self._pending_dir(key)
        record = dict(record)
        record.setdefault("created_at", int(time.time()))
        self._write_file(directory / "setup.json", json.dumps(record, separators=(",", ":")))
        self._write_file(directory / "app.json", json.dumps(app_metadata(meta), separators=(",", ":")))
        self._write_file(directory / "app.pem", private_key)

    def save_active(self, meta: dict[str, Any], private_key: str) -> None:
        """Replace the active configuration while serializing readers in GitHubApp with the same file lock."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        import fcntl

        lock_path = self.root / ".connection.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._write_file(self.root / "app.json", json.dumps(app_metadata(meta), separators=(",", ":")))
            self._write_file(self.root / "app.pem", private_key)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def update_repository_snapshot(self, names: list[str]) -> list[str] | None:
        """Persist repository names in the secret store so later GitHub changes can be audited."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        import fcntl

        lock_path = self.root / ".connection.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            path = self.root / "app.json"
            meta = json.loads(path.read_text(encoding="utf-8"))
            previous = meta.get("repository_names")
            current = sorted(set(names))
            if isinstance(previous, list) and previous == current:
                return list(previous)
            meta["repository_names"] = current
            self._write_file(path, json.dumps(meta, separators=(",", ":")))
            return list(previous) if isinstance(previous, list) else None
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def delete_pending(self, key: str) -> None:
        shutil.rmtree(self._pending_dir(key), ignore_errors=True)

