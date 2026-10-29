"""Test helpers for real PostgreSQL memory tests, isolated from the supplied database."""
from __future__ import annotations

import uuid
import os

try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None
    sql = None

from aiflow.memory.store import Store


def database_url() -> str:
    """Use the run override or the same test-only PostgreSQL default as the v4 fixtures."""
    configured = os.environ.get("AIFLOW4_TEST_DATABASE_URL")
    if configured:
        return configured
    from tests.v4.conftest import DATABASE_URL
    return DATABASE_URL


class MemoryTestDatabase:
    def __init__(self, url: str) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        self.url = url
        self.schema = "memory_test_" + uuid.uuid4().hex
        self.admin = psycopg.connect(url)
        for extension in ("pg_trgm", "unaccent"):
            self.admin.execute(sql.SQL("create extension if not exists {} with schema public").format(sql.Identifier(extension)))
        self.admin.execute(sql.SQL("create schema {}").format(sql.Identifier(self.schema)))
        self.admin.commit()
        self.store = Store(self.connect)
        try:
            self.store.migrate()
        except Exception:
            self.admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(self.schema)))
            self.admin.commit()
            self.admin.close()
            raise

    def connect(self):
        connection = psycopg.connect(self.url)
        connection.execute(sql.SQL("set search_path to {}, public").format(sql.Identifier(self.schema)))
        return connection

    def close(self) -> None:
        self.admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(self.schema)))
        self.admin.commit()
        self.admin.close()
