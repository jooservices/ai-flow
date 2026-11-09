"""PostgreSQL advisory-lock access for per-session test database templates."""
from __future__ import annotations

import psycopg
from psycopg.conninfo import make_conninfo

_TEMPLATE_LOCK_NAMESPACE = 1095327284  # "AIF4"; separate template leases from application advisory locks


class TemplateLease:
    """Hold a session-level PostgreSQL lock until this connection closes."""

    def __init__(self, admin: psycopg.Connection) -> None:
        self._admin = admin

    def close(self) -> None:
        """Release the lease by closing its PostgreSQL session."""
        self._admin.close()


def acquire_template_lease(admin_url: str, name: str) -> TemplateLease | None:
    """Try to lease a database name, returning None when another session already holds it."""
    admin = psycopg.connect(make_conninfo(admin_url, dbname="postgres"), autocommit=True, connect_timeout=10)
    try:
        acquired = admin.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                                 (_TEMPLATE_LOCK_NAMESPACE, name)).fetchone()[0]
    except psycopg.Error:
        admin.close()
        raise
    if not acquired:
        admin.close()
        return None
    return TemplateLease(admin)
