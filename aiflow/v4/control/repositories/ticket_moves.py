"""Persistence queries for audited ticket status moves."""
from __future__ import annotations


class TicketMovesRepository:
    def __init__(self, db) -> None:
        self.db = db

    def recent_for_ticket(self, ticket_key: str, limit: int = 30) -> list[dict]:
        """Recent status moves shown in a ticket's detail drawer."""
        return self.db.all("SELECT ts,from_status,to_status,cause,detail FROM ticket_moves WHERE ticket=%s"
                           " ORDER BY id DESC LIMIT %s", (ticket_key, limit))
