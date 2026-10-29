"""Read persisted context needed to claim and run ticket triage."""
from __future__ import annotations

from .escalations import ACCEPTED_ANSWER_SOURCES, VERIFIED_JIRA_OWNER_SOURCE



def last_dev(db, ticket: str, done: bool = False) -> dict | None:
    outcome = " AND outcome='done'" if done else ""
    return db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'" + outcome
                  + " ORDER BY started_at DESC LIMIT 1", (ticket,))


def latest_verify(db, ticket: str) -> dict | None:
    return db.one("SELECT summary,details FROM runs WHERE ticket_key=%s AND step='verify' AND status<>'running'"
                  " ORDER BY started_at DESC LIMIT 1", (ticket,))


def owner_register(db, project: str) -> list[dict]:
    return db.all("SELECT id,title,decision FROM owner_decisions WHERE kind='decision' AND status='decided'"
                  " AND (project='' OR project=%s) ORDER BY id LIMIT 60", (project,))


def open_escalation(db, ticket: str) -> dict | None:
    return db.one("SELECT * FROM escalations WHERE ticket_key=%s AND resolution='open' ORDER BY id DESC LIMIT 1",
                  (ticket,))


def latest_review_question(db, ticket: str) -> dict | None:
    return db.one("SELECT ts,data FROM events WHERE ticket=%s AND type='review.action'"
                  " AND data->>'action'='ask-ai' ORDER BY ts DESC LIMIT 1", (ticket,))


def latest_triage(db, ticket: str) -> dict | None:
    return db.one("SELECT started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                  " ORDER BY started_at DESC LIMIT 1", (ticket,))


def latest_pr(db, ticket: str) -> dict | None:
    return db.one("SELECT pr_url FROM runs WHERE ticket_key=%s AND pr_url<>'' ORDER BY started_at DESC LIMIT 1",
                  (ticket,))


def owner_answer_state(db, ticket: str) -> dict:
    labels = db.one("SELECT labels FROM tickets WHERE key=%s", (ticket,))
    pending = db.one(
        "SELECT d.id,d.question,d.created_at FROM owner_decisions d WHERE d.kind='decision' AND ("
        "(d.status='asked' AND %s=ANY(d.waiting_tickets)) OR (d.status='decided'"
        " AND (d.decided_by IS NULL OR NOT d.decided_by = ANY(%s)) AND %s=ANY(d.waiting_tickets)))"
        " ORDER BY d.id LIMIT 1",
        (ticket, ACCEPTED_ANSWER_SOURCES, ticket))
    verified = db.one("SELECT d.id,coalesce(e.question,d.question) AS question,d.decision,d.decided_at"
                      " FROM owner_decisions d LEFT JOIN LATERAL (SELECT question FROM escalations"
                      " WHERE ticket_key=%s AND decision_id=d.id ORDER BY id DESC LIMIT 1) e ON true"
                      " WHERE d.kind='decision' AND d.status='decided' AND d.decided_by=ANY(%s)"
                      " AND %s=ANY(d.waiting_tickets) AND NOT EXISTS (SELECT 1 FROM runs r"
                      " WHERE r.ticket_key=%s AND r.step='dev' AND r.started_at>d.decided_at)"
                      " ORDER BY d.decided_at DESC LIMIT 1",
                      (ticket, ACCEPTED_ANSWER_SOURCES, ticket, ticket))
    return {"labels": set((labels or {}).get("labels") or []), "pending": pending, "verified": verified}
