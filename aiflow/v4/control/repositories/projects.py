"""Persistence queries for project lifecycle state."""
from __future__ import annotations


class ProjectsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def lifecycle_rows(self) -> list[dict]:
        """Project close and report state displayed in the project dashboard."""
        return self.db.all("SELECT project,status,closed_at,closed_by,version FROM project_lifecycle")
