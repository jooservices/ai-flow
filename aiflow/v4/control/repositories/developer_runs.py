"""Queries over developer-run history."""
from __future__ import annotations


def timeout_count(db, ticket_key: str) -> int:
    """Return ended developer runs for a ticket that timed out."""
    row = db.one(
        "SELECT count(*) AS count FROM runs WHERE ticket_key=%s AND step='dev'"
        " AND ended_at IS NOT NULL AND (outcome='timeout' OR error_kind='timeout')",
        (ticket_key,),
    )
    return int((row or {}).get("count") or 0)
