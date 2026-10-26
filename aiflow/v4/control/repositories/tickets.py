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

_IDLE_FLOW_CANDIDATES_SQL = """
SELECT key,project,status,blocked_by FROM tickets WHERE NOT ('aiflow-hold'=ANY(labels))
  AND lower(coalesce(type,'')) <> 'epic'
  AND ((status='To Do' AND 'aiflow-ready'=ANY(labels)) OR status='In Verify')
  AND coalesce(jira_updated,synced_at) <= now() - %s * interval '1 minute'
"""

_CAPACITY_READY_SQL = """
SELECT key,project,type,priority,labels,summary,description,fields FROM tickets
WHERE status='To Do' AND 'aiflow-ready'=ANY(labels) AND NOT ('aiflow-hold'=ANY(labels))
  AND lower(coalesce(type,'')) <> 'epic' AND (%s OR project=ANY(%s)) ORDER BY priority,key
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

    def keys_for_projects(self, projects: list[str]) -> set[str]:
        """Ticket keys already mirrored for the Jira projects in this sync pass."""
        return {row["key"] for row in self.db.all("SELECT key FROM tickets WHERE project=ANY(%s)", (projects,))}

    def sync_project_versions(self, project: str, versions: list[dict]) -> None:
        """Mirror Jira's version definitions for one project."""
        for version in versions:
            self.db.run(
                "INSERT INTO versions (id, project, name, released, release_date)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET project=EXCLUDED.project,"
                " name=EXCLUDED.name, released=EXCLUDED.released, release_date=EXCLUDED.release_date",
                (version["id"], project, version["name"], version["released"], version["release_date"]))

    def sync_from_jira(self, ticket: dict) -> None:
        """Persist Jira ticket fields, version links and available change-history facts."""
        done_at = ticket.get("done_at")
        if ticket["status"] == "Done" and done_at is None:
            done_at = ticket["jira_updated"]
        self.db.run(
            "INSERT INTO tickets (key, project, status, type, priority, labels, summary, description, parent_key, blocked_by,"
            " jira_created, jira_updated, done_at, fields, synced_at) VALUES (%(key)s,%(project)s,%(status)s,%(type)s,%(priority)s,%(labels)s,"
            " %(summary)s,%(description)s,%(parent_key)s,%(blocked_by)s,%(jira_created)s,%(jira_updated)s,%(done_at)s,%(fields)s, now())"
            " ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, type=EXCLUDED.type, priority=EXCLUDED.priority,"
            " labels=EXCLUDED.labels, summary=EXCLUDED.summary, description=EXCLUDED.description,"
            " parent_key=EXCLUDED.parent_key, blocked_by=EXCLUDED.blocked_by, jira_updated=EXCLUDED.jira_updated, fields=EXCLUDED.fields,"
            " done_at=CASE WHEN tickets.status <> 'Done' AND EXCLUDED.status = 'Done' THEN EXCLUDED.done_at"
            " WHEN tickets.done_at IS NULL AND EXCLUDED.status = 'Done' THEN EXCLUDED.done_at"
            " ELSE tickets.done_at END, synced_at=now()",
            {**ticket, "done_at": done_at, "fields": J({"assignee": ticket.get("assignee")})})
        self._sync_ticket_versions(ticket)
        self._sync_ticket_history(ticket)

    def _sync_ticket_versions(self, ticket: dict) -> None:
        """Replace Jira's complete Fix version relationship for a ticket."""
        self.db.run(
            "WITH removed AS (DELETE FROM ticket_versions WHERE ticket_key=%s RETURNING ticket_key)"
            " INSERT INTO ticket_versions (ticket_key, version_id)"
            " SELECT %s, fix.version_id FROM unnest(%s::text[]) AS fix(version_id)"
            " CROSS JOIN (SELECT count(*) FROM removed) AS deletion_done"
            " ON CONFLICT (ticket_key, version_id) DO NOTHING",
            (ticket["key"], ticket["key"], ticket["fix_versions"]))

    def _sync_ticket_history(self, ticket: dict) -> None:
        """Persist version attach times and the first In Progress time from Jira history."""
        for version_id, added_at in (ticket.get("fix_version_added") or {}).items():
            self.db.run("INSERT INTO ticket_version_added (ticket_key, version_id, added_at) VALUES (%s,%s,%s)"
                        " ON CONFLICT (ticket_key, version_id) DO UPDATE SET added_at=EXCLUDED.added_at",
                        (ticket["key"], version_id, added_at))
        if ticket.get("first_in_progress"):
            self.db.run("UPDATE tickets SET first_in_progress_at=LEAST(first_in_progress_at, %s) WHERE key=%s",
                        (ticket["first_in_progress"], ticket["key"]))

    def project_epic_rows(self, project: str) -> list[dict]:
        """Ticket rows needed to reconcile parent epic lifecycle state."""
        return self.db.all("SELECT key, project, status, type, parent_key, labels FROM tickets"
                           " WHERE project=%s ORDER BY key", (project,))

    def dashboard_tickets(self) -> list[dict]:
        """Ticket rows used by the Command Center board and epic summaries."""
        return self.db.all("SELECT key, project, status, type, priority, labels, summary, parent_key, blocked_by,"
                           " done_at, jira_updated FROM tickets ORDER BY project, key")

    def idle_flow_candidates(self, older_than_min: float = 0.0) -> list[dict]:
        """Ready or verifying non-epic tickets considered by the idle-flow checks."""
        return self.db.all(_IDLE_FLOW_CANDIDATES_SQL, (older_than_min,))

    def capacity_ready_tickets(self, project_keys: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        """Ready, unheld, non-epic ticket facts used by the capacity plan."""
        return self.db.all(_CAPACITY_READY_SQL, (project_keys is None, list(project_keys or ())))

    def verification_candidates(self, projects: list[str]) -> list[dict]:
        """Tickets in verification that are not on hold or epics."""
        return self.db.all(_VERIFY_CANDIDATES_SQL, (projects,))

    def by_key(self, key: str) -> dict | None:
        """Ticket row for a work item selected by another control-plane query."""
        return self.db.one("SELECT * FROM tickets WHERE key=%s", (key,))

    def epic_key(self, ticket: dict) -> str:
        """Resolve the ticket's parent chain to its Jira epic key."""
        if str(ticket.get("type") or "").strip().lower() == "epic":
            return str(ticket.get("key") or "")
        parent = str(ticket.get("parent_key") or "")
        seen: set[str] = set()
        while parent and parent not in seen:
            seen.add(parent)
            ancestor = self.db.one("SELECT key,type,parent_key FROM tickets WHERE key=%s", (parent,))
            if not ancestor:
                return parent
            if str(ancestor.get("type") or "").strip().lower() == "epic":
                return parent
            parent = str(ancestor.get("parent_key") or "")
        return ""

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
