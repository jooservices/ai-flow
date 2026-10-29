"""Persistence operations for ticket state mirrored from Jira."""
from __future__ import annotations


def lifecycle_ticket(db, ticket: str) -> dict | None:
    return db.one("SELECT key,project FROM tickets WHERE key=%s", (ticket,))


def get(db, ticket: str) -> dict | None:
    return db.one("SELECT * FROM tickets WHERE key=%s", (ticket,))


def verify_candidates(db, projects: list[str]) -> list[dict]:
    return db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
                  " AND NOT ('aiflow-hold' = ANY(labels))", (projects,))


def triage_candidates(db, projects: list[str]) -> list[dict]:
    return db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'"
                  " AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))"
                  " AND EXISTS (SELECT 1 FROM escalations e WHERE e.ticket_key = tickets.key"
                  " AND e.resolution = 'open')", (projects,))


def intake_candidates(db, projects: list[str]) -> list[dict]:
    return db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'"
                  " AND NOT ('aiflow-hold' = ANY(labels)) ORDER BY jira_created NULLS LAST, key", (projects,))


def impact_candidates(db, projects: list[str]) -> list[dict]:
    return db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'"
                  " AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
                  " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key"
                  " AND r.step = 'impact' AND r.outcome = 'assessed')"
                  " ORDER BY jira_created NULLS LAST, key", (projects,))


def development_candidates(db, projects: list[str]) -> list[dict]:
    return db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'To Do'"
                  " AND 'aiflow-ready' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))", (projects,))


def done_keys(db) -> set[str]:
    return {str(row["key"]) for row in db.all("SELECT key FROM tickets WHERE status = 'Done'")}


def update_status(db, ticket: str, status: str) -> None:
    db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, ticket))


def add_label(db, ticket: str, label: str) -> None:
    db.run("UPDATE tickets SET labels=CASE WHEN %s = ANY(labels) THEN labels ELSE array_append(labels,%s) END"
           " WHERE key=%s", (label, label, ticket))


def remove_label(db, ticket: str, label: str) -> None:
    db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, ticket))


def project_open_work(db, project: str, ticket: str) -> list[dict]:
    return db.all("SELECT key,type,status,summary,parent_key FROM tickets WHERE project=%s AND status<>'Done'"
                  " AND key<>%s ORDER BY type<>'Epic',key LIMIT 300", (project, ticket))


def update_description(db, ticket: str, description: str) -> None:
    db.run("UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s", (description, ticket))


def description(db, ticket: str) -> dict | None:
    return db.one("SELECT description FROM tickets WHERE key=%s", (ticket,))
