"""Persistence operations for owner escalation notices and their shared hourly delivery slot."""
from __future__ import annotations

from datetime import datetime, timedelta


def pending_question(db, decision_id: int, ticket: str) -> dict | None:
    return db.one("SELECT d.status,d.waiting_tickets,e.question FROM owner_decisions d"
                  " JOIN escalations e ON e.decision_id=d.id AND e.ticket_key=%s"
                  " AND e.resolution IN ('open','person','waiting') WHERE d.id=%s AND d.kind='decision'"
                  " ORDER BY e.id DESC LIMIT 1", (ticket, decision_id))


def enqueue(db, decision_id: int, ticket: str, question: str, ticket_url: str, now: datetime) -> None:
    db.run("INSERT INTO owner_escalation_notices (decision_id,ticket_key,question,ticket_url,created_at)"
           " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (decision_id,ticket_key) DO NOTHING",
           (decision_id, ticket, question, ticket_url, now))


def claim_due(db, now: datetime, rate_limit: timedelta) -> tuple[str, list[dict]]:
    """Reserve the hourly slot and return the oldest pending rows, if any."""
    with db.pool.connection() as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(74, 255)")
        state = connection.execute("SELECT last_attempt_at FROM owner_escalation_notice_state"
                                   " WHERE id=true FOR UPDATE").fetchone()
        last_attempt = state["last_attempt_at"] if state else None
        if last_attempt and now < last_attempt + rate_limit:
            return "queued", []
        rows = list(connection.execute(
            "SELECT n.id,n.question,n.ticket_url FROM owner_escalation_notices n"
            " JOIN owner_decisions d ON d.id=n.decision_id"
            " WHERE n.sent_at IS NULL AND d.kind='decision' AND d.status='asked'"
            " AND n.ticket_key=ANY(d.waiting_tickets)"
            " AND EXISTS (SELECT 1 FROM escalations e WHERE e.decision_id=d.id"
            " AND e.ticket_key=n.ticket_key AND e.resolution IN ('open','person','waiting'))"
            " ORDER BY n.id LIMIT 100 FOR UPDATE OF n").fetchall())
        if not rows:
            return "empty", []
        connection.execute("UPDATE owner_escalation_notice_state SET last_attempt_at=%s WHERE id=true", (now,))
        return "ready", rows


def mark_sent(db, ids: list[int], now: datetime) -> None:
    with db.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE owner_escalation_notices SET sent_at=%s WHERE id=ANY(%s)", (now, ids))
        connection.execute("UPDATE owner_escalation_notice_state SET last_sent_at=%s WHERE id=true", (now,))
