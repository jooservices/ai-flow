"""All SQL of the ticket mirror used by the tickets feature; it returns `Ticket`, never a raw row."""
from __future__ import annotations

from typing import Any

from .models import Ticket


class TicketRepository:
    def __init__(self, db: Any) -> None:
        self.db = db

    def get(self, key: str) -> Ticket | None:
        row = self.db.one("SELECT key,project,status,type,priority,summary,description,labels,parent_key,blocked_by"
                          " FROM tickets WHERE key=%s", (key,))
        return Ticket.from_row(row) if row else None

    def update_status(self, key: str, status: str) -> None:
        self.db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, key))

    def remove_label(self, key: str, label: str) -> None:
        self.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, key))

    def update_description(self, key: str, description: str) -> None:
        self.db.run("UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s", (description, key))
