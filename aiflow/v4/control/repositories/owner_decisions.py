"""Owner-decision lookups shared by the dashboard state and the review queue."""
from __future__ import annotations

from typing import Any

_BUSINESS_KEYS_SQL = (
    "SELECT DISTINCT ticket_key FROM ("
    "  SELECT ticket_key FROM escalations WHERE resolution='open' "
    "  AND class IN ('new_business_decision', 'irreversible_or_production')"
    "  UNION "
    "  SELECT unnest(waiting_tickets) AS ticket_key FROM owner_decisions WHERE status='asked' "
    "  AND source IN ('owner', 'ask')"
    ") b"
)


def business_decision_ticket_keys(db: Any) -> set[str]:
    """Keys of tickets that wait for a business decision only the owner can make."""
    return {row["ticket_key"] for row in db.all(_BUSINESS_KEYS_SQL)}


def parent_epic_context(db: Any, epic_key: str) -> dict[str, str] | None:
    """The epic's key, summary and description, shown to the AI as the reason behind a child ticket."""
    row = db.one("SELECT key, summary, description FROM tickets WHERE key=%s", (epic_key,))
    if not row:
        return None
    return {"key": row["key"], "summary": row["summary"] or "", "description": row["description"] or ""}
