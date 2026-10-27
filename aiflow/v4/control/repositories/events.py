"""Persistence queries for recent control events."""
from __future__ import annotations


class EventsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def dashboard_recent(self) -> list[dict]:
        """Recent non-heartbeat events shown in the Command Center dashboard."""
        return self.db.all("SELECT ts,type,ticket,account,node,outcome,reason FROM events"
                           " WHERE type <> 'node.heartbeat' ORDER BY id DESC LIMIT 60")

    def ticket_detail_rows(self, ticket_key: str) -> list[dict]:
        """Recent ticket events shown in its detail drawer."""
        return self.db.all("SELECT ts,type,account,node,outcome,reason,data FROM events WHERE ticket=%s"
                           " ORDER BY id DESC LIMIT 80", (ticket_key,))

    def ticket_status_changes(self, ticket_key: str) -> list[dict]:
        """Status and readiness changes used to calculate ticket wait intervals."""
        return self.db.all("SELECT ts,type,outcome,data FROM events WHERE ticket=%s"
                           " AND type=ANY(%s) AND outcome<>'' ORDER BY ts,id",
                           (ticket_key, ["ticket.status", "jira.transition"]))

    def latest_ai_review_question(self, ticket_key: str) -> dict | None:
        return self.db.one("SELECT ts,data FROM events WHERE ticket=%s AND type='review.action' "
                           "AND data->>'action'='ask-ai' ORDER BY ts DESC LIMIT 1", (ticket_key,))
