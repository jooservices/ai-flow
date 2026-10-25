"""Persistence queries for ticket state and parent relationships."""
from __future__ import annotations


class TicketsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def project_epic_rows(self, project: str) -> list[dict]:
        """Ticket rows needed to reconcile parent epic lifecycle state."""
        return self.db.all("SELECT key, project, status, type, parent_key, labels FROM tickets"
                           " WHERE project=%s ORDER BY key", (project,))
