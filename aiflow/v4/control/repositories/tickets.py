"""Persistence queries for ticket state and parent relationships."""
from __future__ import annotations

from ..db import J


class TicketsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def project_epic_rows(self, project: str) -> list[dict]:
        """Ticket rows needed to reconcile parent epic lifecycle state."""
        return self.db.all("SELECT key, project, status, type, parent_key, labels FROM tickets"
                           " WHERE project=%s ORDER BY key", (project,))

    def record_epic_transition(self, key: str, status: str, changed_at, guard: str, reason: str,
                               child_count: int, audit: bool) -> None:
        """Persist the Jira mirror and its transition audit atomically."""
        with self.db.pool.connection() as connection:
            with connection.transaction():
                connection.execute("UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done'"
                                   " THEN %s ELSE done_at END, synced_at=now() WHERE key=%s",
                                   (status, status, changed_at, key))
                if audit:
                    connection.execute(
                        "INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
                        " VALUES ('jira','transition',%s,'control',true,%s,%s)",
                        (key, status, J({"source": "jira_sync", "reason": reason, "child_count": child_count,
                                         "transition_guard": guard})))
