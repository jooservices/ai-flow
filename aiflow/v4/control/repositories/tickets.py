"""Persistence queries for ticket state and parent relationships."""
from __future__ import annotations

from ..db import J


_SLOW_VERIFICATIONS_SQL = """
SELECT t.key, t.project, t.summary, coalesce(d.ended_at, t.jira_updated, t.synced_at) AS since
FROM tickets t LEFT JOIN LATERAL (SELECT ended_at FROM runs WHERE ticket_key=t.key AND step='dev'
  AND outcome='done' ORDER BY started_at DESC LIMIT 1) d ON true
WHERE t.status='In Verify' AND lower(coalesce(t.type,'')) <> 'epic'
  AND NOT ('aiflow-hold' = ANY(t.labels))
  AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.status='running')
  AND coalesce(d.ended_at, t.jira_updated, t.synced_at)
      < coalesce(%s::timestamptz, now()) - %s * interval '1 minute'
ORDER BY since
"""

_VERIFY_CANDIDATES_SQL = """
SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'
  AND NOT ('aiflow-hold' = ANY(labels)) AND lower(coalesce(type,'')) <> 'epic'
"""

_TRIAGE_CANDIDATES_SQL = """
SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'
  AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))
  AND lower(coalesce(type,'')) <> 'epic' AND EXISTS (SELECT 1 FROM escalations e
    WHERE e.ticket_key = tickets.key AND e.resolution = 'open')
"""

_INTAKE_CANDIDATES_SQL = """
SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'
  AND NOT ('aiflow-hold' = ANY(labels)) AND lower(coalesce(type,'')) <> 'epic'
ORDER BY jira_created NULLS LAST, key
"""

_IMPACT_CANDIDATES_SQL = """
SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'
  AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))
  AND lower(coalesce(type,'')) <> 'epic' AND NOT EXISTS (SELECT 1 FROM runs r
    WHERE r.ticket_key = tickets.key AND r.step = 'impact' AND r.outcome = 'assessed')
ORDER BY jira_created NULLS LAST, key
"""

_DEVELOPMENT_CANDIDATES_SQL = """
SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'To Do'
  AND 'aiflow-ready' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))
  AND lower(coalesce(type,'')) <> 'epic'
"""


class TicketsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def project_epic_rows(self, project: str) -> list[dict]:
        """Ticket rows needed to reconcile parent epic lifecycle state."""
        return self.db.all("SELECT key, project, status, type, parent_key, labels FROM tickets"
                           " WHERE project=%s ORDER BY key", (project,))

    def verification_candidates(self, projects: list[str]) -> list[dict]:
        """Tickets in verification that are not on hold or epics."""
        return self.db.all(_VERIFY_CANDIDATES_SQL, (projects,))

    def by_key(self, key: str) -> dict | None:
        """Ticket row for a work item selected by another control-plane query."""
        return self.db.one("SELECT * FROM tickets WHERE key=%s", (key,))

    def triage_candidates(self, projects: list[str]) -> list[dict]:
        """Tickets with an open escalation in the triage queue."""
        return self.db.all(_TRIAGE_CANDIDATES_SQL, (projects,))

    def intake_candidates(self, projects: list[str]) -> list[dict]:
        """Tickets in the intake queue, ordered by age and key."""
        return self.db.all(_INTAKE_CANDIDATES_SQL, (projects,))

    def impact_candidates(self, projects: list[str]) -> list[dict]:
        """Unassessed change requests in the impact queue."""
        return self.db.all(_IMPACT_CANDIDATES_SQL, (projects,))

    def development_candidates(self, projects: list[str]) -> list[dict]:
        """Ready, unheld tickets in the development queue."""
        return self.db.all(_DEVELOPMENT_CANDIDATES_SQL, (projects,))

    def done_keys(self) -> set[str]:
        """Ticket keys that satisfy development blockers."""
        return {row["key"] for row in self.db.all("SELECT key FROM tickets WHERE status = 'Done'")}

    def tickets_in_verify(self) -> list[dict]:
        """Ticket subjects watched for verification exhaustion."""
        return self.db.all("SELECT key, project, status, summary, labels, type FROM tickets WHERE status='In Verify'"
                           " AND lower(coalesce(type,'')) <> 'epic'")

    def waiting_verifications(self, minutes: int, now=None) -> list[dict]:
        """Tickets in verification with no running work for longer than `minutes`."""
        return self.db.all(_SLOW_VERIFICATIONS_SQL, (now, minutes))

    def record_epic_transition(self, key: str, status: str, changed_at, guard: str, reason: str,
                               child_count: int, audit: bool) -> None:
        """Persist the Jira mirror and its transition audit atomically."""
        with self.db.pool.connection() as connection:
            with connection.transaction():
                connection.execute("UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done'"
                                   " THEN %s ELSE done_at END, synced_at=now() WHERE key=%s",
                                   (status, status, changed_at, key))
                if audit:
                    connection.execute(
                        "INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
                        " VALUES ('jira','transition',%s,'control',true,%s,%s)",
                        (key, status, J({"source": "jira_sync", "reason": reason, "child_count": child_count,
                                         "transition_guard": guard})))
