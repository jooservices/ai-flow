"""Per-session test databases (`aiflow4_t_*`): naming and the sweeper that removes the ones left behind.

The pytest fixture creates one database per session and drops it at the end. A session that is killed or times
out never drops it, and each database holds about 95 MB, so the node agent sweeps leftovers: a database that no
connection uses and that is older than two hours goes. The creation time is part of the name
(`aiflow4_t_<epoch>_<hex>`); names from before that change carry no time and go as soon as nothing uses them.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Collection

log = logging.getLogger("aiflow4.testdb")
PREFIX = "aiflow4_t_"
MAX_DATABASES = 8        # idle databases beyond this many are removed once they are ten minutes old
STALE_AFTER_S = 2 * 3600
YOUNG_S = 600
_NAME = re.compile(r"aiflow4_t_(?:(\d{10})_)?[0-9a-f]{8,12}\Z")


def new_name(now: float | None = None) -> str:
    """A fresh database name that carries its creation time."""
    return f"{PREFIX}{int(now if now is not None else time.time())}_{uuid.uuid4().hex[:8]}"


def created_at(name: str) -> int | None:
    """Creation time in the name, or None for a legacy or foreign name."""
    m = _NAME.fullmatch(name)
    return int(m.group(1)) if m and m.group(1) else None


def sweep(admin_url: str, now: float | None = None, stale_after_s: int = STALE_AFTER_S,
          max_databases: int = MAX_DATABASES,
          protected_databases: Collection[str] = ()) -> list[str]:
    """Drop idle test databases, except protected ones, and return their names.

    Needs a role that owns the databases (or a superuser). Protected databases still count toward the configured
    limit but are never dropped by this sweep.
    """
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    now = time.time() if now is None else now
    dropped: list[str] = []
    with psycopg.connect(make_conninfo(admin_url, dbname="postgres"), autocommit=True, connect_timeout=10) as admin:
        rows = admin.execute(
            "SELECT d.datname FROM pg_database d WHERE d.datname LIKE %s AND NOT EXISTS"
            " (SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)", (PREFIX.replace("_", r"\_") + "%",)).fetchall()
        protected = set(protected_databases)
        idle = [r[0] for r in rows if r[0] not in protected and _NAME.fullmatch(r[0])]
        total = admin.execute("SELECT count(*) FROM pg_database WHERE datname LIKE %s",
                              (PREFIX.replace("_", r"\_") + "%",)).fetchone()[0]
        # oldest first; a legacy name has no time and counts as the oldest
        idle.sort(key=lambda n: created_at(n) or 0)
        for name in idle:
            age = now - created_at(name) if created_at(name) else None
            stale = age is None or age >= stale_after_s
            crowded = total - len(dropped) > max_databases and (age is None or age >= YOUNG_S)
            if not (stale or crowded):
                continue
            try:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
                dropped.append(name)
            except psycopg.Error as e:  # a connection arrived meanwhile, or the role does not own it
                log.warning("test database %s not dropped: %s", name, type(e).__name__)
    return dropped
