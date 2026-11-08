"""SQL of the claim path that keeps a ticket with an unresolved hand-off from being claimed again."""
from __future__ import annotations


def lock_ticket_handoff(conn, ticket_key: str) -> None:
    """Serialize claims and hand-off decisions of one ticket for the rest of the transaction."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"ticket-handoff:{ticket_key}",))


def handoff_blocks_claim(conn, ticket_key: str) -> bool:
    """True while a hand-off is blocked or the last three dev or verify runs all ended unsuccessfully."""
    row = conn.execute(
        "SELECT 1 FROM ticket_handoffs WHERE ticket_key=%s AND state='blocked' UNION ALL"
        " SELECT 1 FROM (SELECT outcome FROM runs WHERE ticket_key=%s AND step IN ('dev','verify')"
        " AND status<>'running' AND ended_at IS NOT NULL AND ended_at>coalesce((SELECT resolved_at"
        " FROM ticket_handoffs WHERE ticket_key=%s AND state='resolved'),'-infinity'::timestamptz)"
        " ORDER BY ended_at DESC,started_at DESC,id DESC LIMIT 3) latest"
        " HAVING count(*) FILTER (WHERE outcome IN ('failed','timeout','lost','no_changes'))=3 LIMIT 1",
        (ticket_key, ticket_key, ticket_key)).fetchone()
    return bool(row)
