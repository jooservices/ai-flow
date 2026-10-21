"""Registry database: connection pool and numbered SQL migrations (no ORM)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).with_name("migrations")


class DB:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 10) -> None:
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row},
                                   open=True)

    def close(self) -> None:
        self.pool.close()

    def all(self, sql: str, params: Any = None) -> list[dict]:
        with self.pool.connection() as c:
            return list(c.execute(sql, params).fetchall())

    def one(self, sql: str, params: Any = None) -> dict | None:
        with self.pool.connection() as c:
            return c.execute(sql, params).fetchone()

    def run(self, sql: str, params: Any = None) -> int:
        with self.pool.connection() as c:
            cur = c.execute(sql, params)
            return cur.rowcount

    def migrate(self) -> list[str]:
        """Apply migrations/NNN_*.sql that are not yet recorded; returns the versions applied."""
        applied: list[str] = []
        with self.pool.connection() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r["version"] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.stem in done:
                    continue
                with c.transaction():
                    c.execute(f.read_text(encoding="utf-8"))
                    c.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
                applied.append(f.stem)
        return applied


def J(value: Any) -> Jsonb:
    """Wrap a Python value for a jsonb parameter."""
    return Jsonb(value)


def connect_once(url: str) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row)
