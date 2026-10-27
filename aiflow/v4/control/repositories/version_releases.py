"""Persistence queries for configured Jira version-release runs."""
from __future__ import annotations

from ..db import J


class VersionReleasesRepository:
    def __init__(self, db) -> None:
        self.db = db

    def status(self, project: str, version_id: str, repository: str) -> dict | None:
        return self.db.one("SELECT status FROM version_releases WHERE project=%s AND version_id=%s AND repository=%s",
                           (project, version_id, repository))

    def mark_checking(self, project, version: dict, tracker: str, snapshot: dict) -> None:
        self.db.run("INSERT INTO version_releases (project,version_id,version_name,tracker_key,status,gate_detail,"
                    "ticket_snapshot,repository) VALUES (%s,%s,%s,%s,'checking','',%s,%s) "
                    "ON CONFLICT (project,version_id,repository) DO UPDATE SET "
                    "version_name=EXCLUDED.version_name,tracker_key=EXCLUDED.tracker_key,status='checking',"
                    "gate_detail='',ticket_snapshot=EXCLUDED.ticket_snapshot,updated_at=now()",
                    (project.key, version["id"], version["name"], tracker, J(snapshot["tickets"]), project.repo))
