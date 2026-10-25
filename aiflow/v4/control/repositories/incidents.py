"""Persistence queries for incident state."""
from __future__ import annotations


class IncidentsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def open_subjects(self, kind: str) -> list[str]:
        """Subjects with an open incident of `kind`."""
        return [row["subject"] for row in self.db.all(
            "SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (kind,))]
