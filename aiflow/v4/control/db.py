"""Registry database: connection pool and numbered SQL migrations (no ORM)."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).with_name("migrations")


class DB:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 10) -> None:
        self.url = url
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row, "application_name": "aiflow4-control"}, open=True)
        self._active_connection: ContextVar[psycopg.Connection | None] = ContextVar(
            f"aiflow4_db_connection_{id(self)}", default=None)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Share one transaction across DB helpers in this context, using a savepoint when nested."""
        active = self._active_connection.get()
        if active is not None:
            with active.transaction():
                yield active
            return
        with self.pool.connection() as c:
            token = self._active_connection.set(c)
            try:
                with c.transaction():
                    yield c
            finally:
                self._active_connection.reset(token)

    def all(self, sql: str, params: Any = None) -> list[dict]:
        active = self._active_connection.get()
        if active is not None:
            return list(active.execute(sql, params).fetchall())
        with self.pool.connection() as c:
            return list(c.execute(sql, params).fetchall())

    def one(self, sql: str, params: Any = None) -> dict | None:
        active = self._active_connection.get()
        if active is not None:
            return active.execute(sql, params).fetchone()
        with self.pool.connection() as c:
            return c.execute(sql, params).fetchone()

    def run(self, sql: str, params: Any = None) -> int:
        active = self._active_connection.get()
        if active is not None:
            return active.execute(sql, params).rowcount
        with self.pool.connection() as c:
            cur = c.execute(sql, params)
            return cur.rowcount

    def migrate(self, directory: Path | None = None) -> list[str]:
        """Apply migrations/NNN_*.sql that are not yet recorded; returns the versions applied.

        Forward only (docs/v4/fleet-upgrade.md): recorded versions this release does not ship (a newer release
        applied them before a rollback) are left alone, and nothing is ever undone. The checksum of each applied
        file is kept so the pre-upgrade check can refuse a release that edits an applied migration."""
        applied: list[str] = []
        with self.pool.connection() as c:
            # control-api and control-loops start together; one migrates at a time (session advisory lock)
            c.execute("SELECT pg_advisory_lock(424204)")
            try:
                c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY,"
                          " applied_at timestamptz NOT NULL DEFAULT now())")
                c.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text NOT NULL DEFAULT ''")
                done = {r["version"]: r["checksum"]
                        for r in c.execute("SELECT version, checksum FROM schema_migrations").fetchall()}
                for f in sorted((directory or MIGRATIONS).glob("*.sql")):
                    if f.stem in done:
                        if not done[f.stem]:  # applied before checksums were recorded
                            c.execute("UPDATE schema_migrations SET checksum=%s WHERE version=%s",
                                      (migration_checksum(f), f.stem))
                        continue
                    with c.transaction():
                        c.execute(f.read_text(encoding="utf-8"))
                        c.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                                  (f.stem, migration_checksum(f)))
                    applied.append(f.stem)
                # the role the control plane logs in with is the product; any other role is an operator (AC6)
                if c.execute("SELECT to_regclass('audit_product_roles') AS t").fetchone()["t"]:
                    c.execute("INSERT INTO audit_product_roles (role_name) VALUES (session_user) ON CONFLICT DO NOTHING")
                # tables added by later migrations get the operator audit trigger too (AC6)
                if c.execute("SELECT to_regproc('aiflow4_attach_audit_triggers') AS f").fetchone()["f"]:
                    c.execute("SELECT aiflow4_attach_audit_triggers()")
            finally:
                c.execute("SELECT pg_advisory_unlock(424204)")
        return applied


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def J(value: Any) -> Jsonb:
    """Wrap a Python value for a jsonb parameter."""
    return Jsonb(value)


def connect_once(url: str) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row)
