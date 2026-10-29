"""Persistence operations for escalation records and owner decisions."""
from __future__ import annotations

from contextlib import contextmanager

from ..db import J

VERIFIED_JIRA_OWNER_SOURCE = "verified-jira-comment"
DEADLINE_SOURCE = "deadline"  # the recommendation applied after the owner-question deadline (JAIF-370)
ACCEPTED_ANSWER_SOURCES = [VERIFIED_JIRA_OWNER_SOURCE, DEADLINE_SOURCE]  # answers that let a waiting ticket continue


def _one(db, query: str, params: tuple = ()):
    return db.one(query, params) if hasattr(db, "one") else db.execute(query, params).fetchone()


def _all(db, query: str, params: tuple = ()) -> list[dict]:
    return db.all(query, params) if hasattr(db, "all") else list(db.execute(query, params).fetchall())


def _run(db, query: str, params: tuple = ()) -> None:
    db.run(query, params) if hasattr(db, "run") else db.execute(query, params)


@contextmanager
def owner_decision_transaction(db):
    """Serialize matching and insertion of an owner question across control workers."""
    with db.pool.connection() as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(255, 255)")
        yield connection


def decision_candidates(db, status: str, project: str) -> list[dict]:
    return _all(db, "SELECT * FROM owner_decisions WHERE kind='decision' AND status=%s AND (project='' OR project=%s)"
                " ORDER BY id", (status, project))


def decision_by_question_key(db, status: str, question_key: str, project: str) -> dict | None:
    if not question_key:
        return None
    return _one(db, "SELECT * FROM owner_decisions WHERE kind='decision' AND status=%s AND question_key=%s"
                " AND (project='' OR project=%s) ORDER BY id LIMIT 1", (status, question_key, project))


def pending_decision_for_ticket(db, ticket: str) -> dict | None:
    return _one(db, "SELECT * FROM owner_decisions WHERE status='asked' AND %s=ANY(waiting_tickets)", (ticket,))


def topic_exists(db, topic: str) -> bool:
    return _one(db, "SELECT 1 AS x FROM owner_decisions WHERE topic=%s", (topic,)) is not None


def insert_decision(db, topic: str, title: str, question: str, decision: str, patterns: list[str], project: str,
                    source: str, by: str) -> dict:
    return _one(db, "INSERT INTO owner_decisions (topic,title,question,decision,patterns,project,source,decided_by,decided_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING *",
                (topic, title[:200], question[:2000], decision[:3000], J(patterns), project, source, by))


def insert_asked_decision(db, topic: str, title: str, question: str, options: list[str], recommended: str,
                          patterns: list[str], ticket: str, question_key: str) -> dict:
    return _one(db, "INSERT INTO owner_decisions (topic,title,question,options,recommended,patterns,project,status,source,"
                "ticket_key,waiting_tickets,question_key) VALUES (%s,%s,%s,%s,%s,%s,%s,'asked','ask',%s,%s,%s) RETURNING *",
                (topic, title[:200], question[:3000], J(options), recommended[:500], J(patterns), "", ticket,
                 [ticket], question_key))


def decision_by_id(db, decision_id: int) -> dict | None:
    return _one(db, "SELECT * FROM owner_decisions WHERE id=%s", (decision_id,))


def resuming_decisions(db, verified_source: str) -> list[dict]:
    return _all(db, "SELECT DISTINCT d.* FROM owner_decisions d JOIN escalations e ON e.decision_id=d.id"
                " WHERE d.status='decided' AND d.decided_by=%s AND e.resolution='resuming' ORDER BY d.id",
                (verified_source,))


def resuming_tickets(db, decision_id: int) -> list[dict]:
    return _all(db, "SELECT DISTINCT ticket_key FROM escalations WHERE decision_id=%s AND resolution='resuming'"
                " ORDER BY ticket_key", (decision_id,))


def record_escalation(db, ticket: str, project: str, source: str, run_id: str, cls: str, question: str,
                      reason: str, resolution: str, decision_id: int | None, answer: str,
                      reached_person: bool) -> dict:
    return _one(db, "INSERT INTO escalations (ticket_key,project,source,run_id,class,question,reason,decision_id,"
                "resolution,reached_person,answer,resolved_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "CASE WHEN %s IN ('open','waiting') THEN NULL ELSE now() END) RETURNING *",
                (ticket, project, source, run_id, cls, question[:3000], reason[:500], decision_id, resolution,
                 reached_person, answer[:3000], resolution))


def pending_answer(db, ticket: str) -> dict | None:
    return _one(db, "SELECT question,answer,created_at FROM escalations WHERE ticket_key=%s AND answer<>''"
                " AND resolution IN ('register','policy','machine','amendment')"
                " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=escalations.ticket_key AND r.step='dev'"
                " AND r.started_at > escalations.created_at AND r.outcome IN ('done','blocked','auto_answered'))"
                " ORDER BY id DESC LIMIT 1", (ticket,))


def metrics_since(db, since) -> list[dict]:
    return _all(db, "SELECT id,ticket_key,project,class,resolution,reached_person,created_at,question FROM escalations"
                " WHERE created_at >= %s ORDER BY created_at", (since,))


def amendment_for_ticket(db, ticket: str, criterion: str) -> dict | None:
    return _one(db, "SELECT * FROM owner_decisions WHERE kind='amendment' AND ticket_key=%s AND title LIKE %s",
                (ticket, f"{criterion.upper()} %"))


def insert_pending_amendment(db, topic: str, title: str, reason: str, project: str, ticket: str, by: str) -> dict:
    return _one(db, "INSERT INTO owner_decisions (topic,kind,title,question,decision,project,source,ticket_key,decided_by,decided_at)"
                " VALUES (%s,'amendment',%s,%s,'pending',%s,'amendment',%s,%s,now()) RETURNING *",
                (topic, title, reason[:2000], project, ticket, by))


def delete_pending_amendment(db, decision_id: int) -> None:
    db.run("DELETE FROM owner_decisions WHERE id=%s AND kind='amendment' AND decision='pending'", (decision_id,))


def recent_amendments(db, since) -> list[dict]:
    return _all(db, "SELECT id,ticket_key,project,title,decision,question,created_at FROM owner_decisions"
                " WHERE kind='amendment' AND created_at >= %s ORDER BY id DESC LIMIT 100", (since,))


def register_entries(db) -> list[dict]:
    return _all(db, "SELECT * FROM owner_decisions WHERE status<>'retired' ORDER BY id DESC LIMIT 500")


def resuming_register_entries(db) -> list[dict]:
    return _all(db, "SELECT DISTINCT d.*, ARRAY(SELECT DISTINCT pending.ticket_key FROM escalations pending"
                " WHERE pending.decision_id=d.id AND pending.resolution='resuming' ORDER BY pending.ticket_key)"
                " AS resuming_tickets FROM owner_decisions d JOIN escalations e ON e.decision_id=d.id"
                " WHERE d.status='decided' AND e.resolution='resuming' ORDER BY d.id DESC LIMIT 100")

def increment_citation(db, decision_id: int) -> None:
    _run(db, "UPDATE owner_decisions SET cited_count=cited_count+1, last_cited_at=now() WHERE id=%s", (decision_id,))


def requires_jira_owner_answer(db, decision_id: int, waiting_tickets: list[str]) -> bool:
    row = db.one("SELECT EXISTS(SELECT 1 FROM escalations WHERE decision_id=%s)"
                 " OR EXISTS(SELECT 1 FROM tickets WHERE key=ANY(%s)) AS present", (decision_id, waiting_tickets))
    return bool(row and row["present"])


def pending_owner_decisions(db, ticket: str) -> list[dict]:
    return db.all(
        "SELECT d.id,d.question,d.options,d.recommended,d.waiting_tickets,d.ticket_key<>%s AS grouped,"
        "e.id AS escalation_id,e.question_comment_at,e.legacy_question_comment,e.created_at AS escalation_created_at,"
        "e.question AS ticket_question,e.run_id FROM owner_decisions d LEFT JOIN LATERAL ("
        "SELECT id,question_comment_at,legacy_question_comment,created_at,question,run_id FROM escalations"
        " WHERE ticket_key=%s AND decision_id=d.id ORDER BY id DESC LIMIT 1) e ON true"
        " WHERE d.kind='decision' AND %s=ANY(d.waiting_tickets) AND (d.status='asked' OR"
        " (d.status='decided' AND (d.decided_by IS NULL OR NOT d.decided_by = ANY(%s)))) ORDER BY d.id",
        (ticket, ticket, ticket, ACCEPTED_ANSWER_SOURCES))


def latest_question_for_decision(db, ticket: str, decision_id: int) -> dict | None:
    return db.one("SELECT question FROM escalations WHERE ticket_key=%s AND decision_id=%s"
                  " ORDER BY id DESC LIMIT 1", (ticket, decision_id))


def owner_question_comment_state(db, ticket: str, decision_id: int, run_id: str) -> dict | None:
    return db.one(
        "SELECT e.id,e.question_comment_at,e.legacy_question_comment,e.created_at AS escalation_created_at,"
        "e.question AS ticket_question,d.options,d.recommended,d.ticket_key<>e.ticket_key AS grouped"
        " FROM escalations e JOIN owner_decisions d ON d.id=e.decision_id"
        " WHERE e.ticket_key=%s AND e.decision_id=%s AND e.run_id=%s"
        " AND e.resolution IN ('open','person','waiting') AND d.status='asked' ORDER BY e.id DESC LIMIT 1",
        (ticket, decision_id, run_id))


def record_recovered_question_comment(db, escalation_id: int, created_at) -> dict | None:
    return db.one("UPDATE escalations SET question_comment_at=coalesce(question_comment_at,%s),"
                  "legacy_question_comment=false WHERE id=%s RETURNING question_comment_at",
                  (created_at, escalation_id))


def record_owner_question_comment(db, ticket: str, decision_id: int, run_id: str, created_at) -> dict | None:
    return db.one(
        "UPDATE escalations SET question_comment_at=%s,legacy_question_comment=false"
        " WHERE id=(SELECT e.id FROM escalations e JOIN owner_decisions d ON d.id=e.decision_id"
        " WHERE e.ticket_key=%s AND e.decision_id=%s AND e.run_id=%s"
        " AND e.resolution IN ('open','person','waiting') AND d.status='asked'"
        " ORDER BY e.id DESC LIMIT 1) RETURNING id,question_comment_at",
        (created_at, ticket, decision_id, run_id))


def add_waiting_ticket(connection, decision_id: int, ticket: str) -> None:
    connection.execute("UPDATE owner_decisions SET waiting_tickets=CASE WHEN %s = ANY(waiting_tickets) THEN waiting_tickets"
                       " ELSE array_append(waiting_tickets,%s) END WHERE id=%s", (ticket, ticket, decision_id))


def update_open_escalation(db, escalation_id: int, cls: str, reason: str, resolution: str, decision_id: int | None,
                           answer: str, reached_person: bool, run_id: str) -> dict | None:
    return db.one("UPDATE escalations SET class=%s,reason=%s,resolution=%s,decision_id=coalesce(%s,decision_id),"
                  "answer=%s,reached_person=%s,run_id=%s,"
                  "resolved_at=CASE WHEN %s IN ('open','waiting') THEN NULL ELSE now() END"
                  " WHERE id=%s AND resolution='open' RETURNING *",
                  (cls, reason[:500], resolution, decision_id, answer[:3000], reached_person, run_id,
                   resolution, escalation_id))


def update_open_question(db, escalation_id: int, run_id: str, question: str, cls: str, reason: str) -> dict | None:
    return db.one("UPDATE escalations SET question=%s,class=%s,reason=%s,run_id=%s"
                  " WHERE id=%s AND resolution='open' RETURNING *",
                  (question[:3000], cls, reason, run_id, escalation_id))


def prepare_owner_handoff(db, ticket: str, run_id: str, cls: str, reason: str, decision_id: int) -> dict | None:
    return db.one("UPDATE escalations SET class=%s,reason=%s,decision_id=%s,run_id=%s,resolved_at=NULL"
                  " WHERE id=(SELECT id FROM escalations WHERE ticket_key=%s AND resolution='open'"
                  " ORDER BY id DESC LIMIT 1) AND resolution='open' RETURNING *",
                  (cls, reason[:500], decision_id, run_id, ticket))


def complete_owner_handoff(db, ticket: str, decision_id: int, run_id: str) -> dict | None:
    updated = db.one("UPDATE escalations e SET resolution=CASE WHEN d.ticket_key=e.ticket_key THEN 'person' ELSE 'waiting' END,"
                     "resolved_at=now(),reached_person=true"
                     " FROM owner_decisions d,tickets t WHERE e.ticket_key=%s AND e.decision_id=d.id AND d.id=%s"
                     " AND t.key=e.ticket_key AND e.question_comment_at IS NOT NULL"
                     " AND 'needs-owner'=ANY(t.labels) AND 'owner-escalated'=ANY(t.labels)"
                     " AND d.status='asked' AND %s=ANY(d.waiting_tickets) AND e.run_id=%s"
                     " AND e.resolution='open' RETURNING e.*", (ticket, decision_id, ticket, run_id))
    return updated or db.one("SELECT * FROM escalations WHERE ticket_key=%s AND decision_id=%s AND run_id=%s"
                             " AND resolution IN ('person','waiting') ORDER BY id DESC LIMIT 1",
                             (ticket, decision_id, run_id))


def store_owner_answer(db, decision_id: int, text: str, by: str) -> dict:
    with db.pool.connection() as connection, connection.transaction():
        row = connection.execute("SELECT * FROM owner_decisions WHERE id=%s FOR UPDATE", (decision_id,)).fetchone()
        if not row or row["status"] not in ("asked", "decided"):
            raise LookupError("this question is not waiting for an answer")
        was_asked = row["status"] == "asked"
        stale_command_center_answer = row["status"] == "decided" and row["decided_by"] != by
        if was_asked or stale_command_center_answer:
            row = connection.execute(
                "UPDATE owner_decisions SET status='decided',decision=%s,decided_by=%s,decided_at=now()"
                " WHERE id=%s AND (status='asked' OR (status='decided' AND decided_by IS DISTINCT FROM %s))"
                " RETURNING *", (text[:3000], by, decision_id, by)).fetchone()
            if not row:
                raise LookupError("this question was answered concurrently")
        pending = connection.execute(
            "UPDATE escalations SET answer=%s,resolution='resuming',reached_person=true,resolved_at=NULL"
            " WHERE decision_id=%s AND resolution IN ('open','person','waiting','resuming') RETURNING id",
            (row["decision"], decision_id)).fetchall()
        if not was_asked and not pending:
            raise LookupError("this question is not waiting for an answer")
        return row


def finish_resumption(db, ticket: str, decision_id: int, answer: str) -> None:
    db.run("UPDATE escalations SET answer=%s,resolution='person',reached_person=true,resolved_at=now()"
           " WHERE ticket_key=%s AND decision_id=%s AND resolution='resuming'", (answer, ticket, decision_id))


def retire_decision(db, decision_id: int, verified_source: str) -> dict | None:
    return db.one(
        "UPDATE owner_decisions d SET status='retired' WHERE d.id=%s AND NOT ("
        "d.kind='decision' AND (d.status='asked' OR (d.status='decided' AND d.decided_by IS DISTINCT FROM %s)) AND ("
        "EXISTS (SELECT 1 FROM escalations e WHERE e.decision_id=d.id) OR"
        " EXISTS (SELECT 1 FROM tickets t WHERE t.key=ANY(d.waiting_tickets)))) RETURNING d.*",
        (decision_id, verified_source))


def update_decision(db, decision_id: int, decision: str) -> dict | None:
    return db.one("UPDATE owner_decisions SET decision=%s WHERE id=%s RETURNING *", (decision[:3000], decision_id))
