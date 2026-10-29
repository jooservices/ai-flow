"""The development run a verify, QA or rework step builds on."""
from __future__ import annotations


def delivered_or_last(db, key: str) -> dict | None:
    """The latest finished dev run that delivered work, else the latest finished one, in one query."""
    return db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
                  " ORDER BY (outcome='done') DESC, started_at DESC LIMIT 1", (key,))
