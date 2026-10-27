"""Persistence queries for accepted and refused control-plane writes."""
from __future__ import annotations


class AuditsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def ticket_writes(self, ticket_key: str) -> list[dict]:
        """Recent audited writes shown in a ticket's detail drawer."""
        return self.db.all("SELECT ts,target,action,actor,fence,accepted,result FROM write_audit WHERE ticket=%s"
                           " ORDER BY id DESC LIMIT 40", (ticket_key,))
