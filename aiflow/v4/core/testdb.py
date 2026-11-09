"""Per-session test databases (`aiflow4_t_*`): naming and the sweeper that removes the ones left behind.

The pytest session migrates one template database once; every test gets a copy of it (`create_from_template`) and
drops the copy at its end. A session that is killed or times out never drops its databases, and each database holds about 95 MB, so the node agent sweeps leftovers: a database that no
connection uses and that is older than two hours goes. The creation time is part of the name
(`aiflow4_t_<epoch>_<hex>`); names from before that change carry no time and go as soon as nothing uses them.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Collection
from concurrent.futures import Future, ThreadPoolExecutor

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

log = logging.getLogger("aiflow4.testdb")
PREFIX = "aiflow4_t_"
MAX_DATABASES = 8        # idle databases beyond this many are removed once they are ten minutes old
STALE_AFTER_S = 2 * 3600
YOUNG_S = 600
COPY_WAIT_S = 10.0       # how long a copy waits for a template that is still in use
_NAME = re.compile(r"aiflow4_t_(?:(\d{10})_)?[0-9a-f]{8,12}\Z")


def new_name(now: float | None = None) -> str:
    """A fresh database name that carries its creation time."""
    return f"{PREFIX}{int(now if now is not None else time.time())}_{uuid.uuid4().hex[:8]}"


def created_at(name: str) -> int | None:
    """Creation time in the name, or None for a legacy or foreign name."""
    m = _NAME.fullmatch(name)
    return int(m.group(1)) if m and m.group(1) else None


def url_for(url: str, database: str) -> str:
    """The same server and credentials as `url`, pointing at another database."""
    return make_conninfo(url, dbname=database)


def create_from_template(admin_url: str, template: str | None, name: str | None = None,
                         wait_s: float = COPY_WAIT_S) -> str:
    """Create database `name` (a fresh test name by default) as a copy of `template`, or empty when it is None.

    PostgreSQL refuses to copy a database that has a connection, and two copies of one template cannot run at the
    same moment, so a refusal is retried for up to `wait_s` seconds."""
    name = name or new_name()
    statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
    if template:
        statement += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
    deadline = time.monotonic() + wait_s
    with psycopg.connect(url_for(admin_url, "postgres"), autocommit=True, connect_timeout=10) as admin:
        while True:
            try:
                admin.execute(statement)
                return name
            except psycopg.errors.ObjectInUse:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)


def drop(admin_url: str, name: str) -> None:
    """Drop a test database, ending any connection still open to it."""
    with psycopg.connect(url_for(admin_url, "postgres"), autocommit=True, connect_timeout=10) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


class BackgroundDropper:
    """Drops test databases on one background thread: DROP DATABASE waits for a server checkpoint (seconds on a busy
    server), and the test that owned the database has no reason to wait for it. `wait` returns when all are gone."""

    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aiflow4-testdb-drop")
        self._pending: list[Future] = []

    def submit(self, name: str) -> None:
        self._pending.append(self._worker.submit(drop, self._admin_url, name))

    def wait(self) -> None:
        """Wait for every submitted drop, then stop the thread; the first failed drop is raised."""
        try:
            for future in self._pending:
                future.result()
        finally:
            self._worker.shutdown()


def sweep(admin_url: str, now: float | None = None, stale_after_s: int = STALE_AFTER_S,
          max_databases: int = MAX_DATABASES,
          protected_databases: Collection[str] = (), only: Collection[str] | None = None) -> list[str]:
    """Drop idle test databases, except protected ones, and return their names.

    `only` limits the candidates to the given names, so a test of the sweeper cannot drop the database of another
    pytest session that shares the server.

    Needs a role that owns the databases (or a superuser). Protected databases still count toward the configured
    limit but are never dropped by this sweep.
    """
    now = time.time() if now is None else now
    dropped: list[str] = []
    with psycopg.connect(url_for(admin_url, "postgres"), autocommit=True, connect_timeout=10) as admin:
        rows = admin.execute(
            "SELECT d.datname FROM pg_database d WHERE d.datname LIKE %s AND NOT EXISTS"
            " (SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)", (PREFIX.replace("_", r"\_") + "%",)).fetchall()
        protected = set(protected_databases)
        idle = [r[0] for r in rows if r[0] not in protected and _NAME.fullmatch(r[0])
                and (only is None or r[0] in only)]
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
