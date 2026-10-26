"""Persistence queries for incident state."""
from __future__ import annotations


class IncidentsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def open_subjects(self, kind: str) -> list[str]:
        """Subjects with an open incident of `kind`."""
        return [row["subject"] for row in self.db.all(
            "SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (kind,))]

    def dashboard_open(self) -> list[dict]:
        """Open incidents displayed in the Command Center dashboard."""
        return self.db.all("SELECT id,opened_at,severity,kind,subject,title,detail FROM incidents"
                           " WHERE closed_at IS NULL ORDER BY opened_at DESC")

    def dashboard_recently_closed(self) -> list[dict]:
        """Incidents closed during the last day for the dashboard's recent history."""
        return self.db.all("SELECT id,opened_at,closed_at,severity,kind,subject,title,detail FROM incidents"
                           " WHERE closed_at > now() - interval '24 hours' ORDER BY closed_at DESC LIMIT 20")
