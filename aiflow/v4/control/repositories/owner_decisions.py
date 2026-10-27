"""Persistence queries for the owner decision register."""
from __future__ import annotations


class OwnerDecisionsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def decided_register(self, project: str) -> list[dict]:
        return self.db.all("SELECT id,title,decision FROM owner_decisions WHERE kind='decision' "
                           "AND status='decided' AND (project='' OR project=%s) ORDER BY id LIMIT 60",
                           (project,))
