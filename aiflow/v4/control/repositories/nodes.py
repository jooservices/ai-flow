"""Persistence queries for node state used by control services."""
from __future__ import annotations


class NodesRepository:
    def __init__(self, db) -> None:
        self.db = db

    def candidate_node(self, node: str) -> dict:
        """Node data needed to decide whether an account can verify a ticket there."""
        return self.db.one("SELECT name,status,last_heartbeat,projects,dynamic,tools,metrics,accepting_work,"
                           "release_version FROM nodes WHERE name=%s AND removed_at IS NULL", (node,)) or {}
