"""Persistence queries for recent control events."""
from __future__ import annotations


class EventsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def dashboard_recent(self) -> list[dict]:
        """Recent non-heartbeat events shown in the Command Center dashboard."""
        return self.db.all("SELECT ts,type,ticket,account,node,outcome,reason FROM events"
                           " WHERE type <> 'node.heartbeat' ORDER BY id DESC LIMIT 60")
