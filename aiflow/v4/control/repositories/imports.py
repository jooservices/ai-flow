"""`control_imports`: the marker that records a one-time import, with the revision it was taken from."""
from __future__ import annotations

from ..db import J


def is_done(db, name: str) -> bool:
    return bool(db.one("SELECT 1 AS done FROM control_imports WHERE name=%s", (name,)))


def claim(conn, name: str) -> bool:
    """True for exactly one caller: the one that inserted the marker."""
    return bool(conn.execute("INSERT INTO control_imports (name) VALUES (%s) ON CONFLICT DO NOTHING RETURNING name",
                             (name,)).fetchone())


def finish(conn, name: str, detail: dict) -> None:
    conn.execute("UPDATE control_imports SET detail=%s WHERE name=%s", (J(detail), name))


def detail(db, name: str) -> dict:
    row = db.one("SELECT detail FROM control_imports WHERE name=%s", (name,))
    return dict(row["detail"] or {}) if row else {}


def merge_detail(db, name: str, extra: dict) -> None:
    db.run("UPDATE control_imports SET detail=detail || %s WHERE name=%s", (J(extra), name))
