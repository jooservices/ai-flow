"""Persistence operations for first-run setup: wizard state, the first Administrator and the sample ticket."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable

from ..db import J

STATE_COLUMNS = "public_url,skipped_steps,sample_ticket_key,sample_confirmed_at,completed_at"
SAMPLE_LOCK = "aiflow4-setup-sample-ticket"
FIRST_ADMIN_LOCK = "aiflow4-first-admin"
SAMPLE_SUMMARY = "AI Flow setup sample ticket"
SAMPLE_BODY = "Sample ticket created by the AI Flow first-run setup wizard."
EMPTY_STATE = {"public_url": "", "skipped_steps": [], "sample_ticket_key": "", "sample_confirmed_at": None,
               "completed_at": None}


def user_count(db) -> int:
    return int(db.one("SELECT count(*) AS count FROM console_users")["count"])


def state(db) -> dict:
    """The single wizard state row, created on first read."""
    row = db.one(f"SELECT {STATE_COLUMNS} FROM setup_wizard_state WHERE id=true")
    if not row:
        db.run("INSERT INTO setup_wizard_state (id) VALUES (true) ON CONFLICT (id) DO NOTHING")
        row = db.one(f"SELECT {STATE_COLUMNS} FROM setup_wizard_state WHERE id=true")
    return row or dict(EMPTY_STATE)


def stored_public_url(db) -> str:
    row = db.one("SELECT public_url FROM setup_wizard_state WHERE id=true")
    return str(row["public_url"]) if row and row.get("public_url") else ""


def mark_completed(db) -> None:
    db.run("UPDATE setup_wizard_state SET completed_at=now(),updated_at=now() WHERE id=true AND completed_at IS NULL")


def worker_node_names(db) -> set[str]:
    return {str(item["name"]) for item in
            db.all("SELECT name FROM nodes WHERE role='worker' AND removed_at IS NULL ORDER BY name")}


def account_rows(db) -> list[dict]:
    return db.all("SELECT id,node,status FROM accounts ORDER BY id")


def set_step_skipped(db, step_id: str, skipped: bool, actor: str) -> None:
    """Persist a skip or resume and its audit row as one database transaction."""
    action = "skip_step" if skipped else "resume_step"
    result = "setup step skipped" if skipped else "setup step resumed"
    with db.pool.connection() as connection:
        with connection.transaction():
            if skipped:
                connection.execute(
                    "UPDATE setup_wizard_state SET skipped_steps=array_append(array_remove(skipped_steps,%s),%s),"
                    "updated_at=now() WHERE id=true", (step_id, step_id))
            else:
                connection.execute("UPDATE setup_wizard_state SET skipped_steps=array_remove(skipped_steps,%s),"
                                   "updated_at=now() WHERE id=true", (step_id,))
            connection.execute("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
                               " VALUES ('setup',%s,'',%s,true,%s,%s)",
                               (action, actor, result, J({"step": step_id})))


class FirstAdminRefused(Exception):
    """The first Administrator cannot be created; `reason` is `exists` or `claimed`."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def create_first_admin(db, public_url: str, email: str, password_hash: str,
                       admission: Callable[[object], None]) -> None:
    """Claim ownership, create the Administrator, save the address and audit it in one locked transaction.

    `admission` runs inside the transaction with its connection so the licence check sees the same user rows."""
    with db.pool.connection() as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (FIRST_ADMIN_LOCK,))
            if connection.execute("SELECT 1 FROM console_users LIMIT 1").fetchone():
                raise FirstAdminRefused("exists")
            admission(connection)
            claimed = connection.execute(
                "UPDATE setup_wizard_state SET ownership_claimed_at=now(),updated_at=now()"
                " WHERE id=true AND ownership_claimed_at IS NULL RETURNING id").fetchone()
            if not claimed:
                raise FirstAdminRefused("claimed")
            connection.execute("INSERT INTO console_users (email,display_name,role,roles,password_hash)"
                               " VALUES (%s,'','administrator',ARRAY['administrator']::text[],%s)",
                               (email, password_hash))
            connection.execute("UPDATE setup_wizard_state SET public_url=%s,updated_at=now() WHERE id=true",
                               (public_url,))
            connection.execute("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
                               " VALUES ('setup','bootstrap','',%s,true,'first Administrator created',%s)",
                               (email, J({"public_url": public_url})))


def active_user_count(connection) -> int:
    return int(connection.execute("SELECT count(*) AS count FROM console_users WHERE disabled=false"
                                  ).fetchone()["count"])


@contextmanager
def sample_ticket_lock(db):
    """Hold an installation-wide PostgreSQL lock while a request re-reads and confirms the sample issue."""
    with db.pool.connection() as connection:
        connection.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (SAMPLE_LOCK,))
        connection.commit()
        try:
            yield connection
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (SAMPLE_LOCK,))
            connection.commit()


def lock_sample_state(connection) -> dict:
    return connection.execute("SELECT completed_at,sample_ticket_key,sample_confirmed_at"
                              " FROM setup_wizard_state WHERE id=true FOR UPDATE").fetchone()


def store_sample_key(connection, key: str) -> None:
    connection.execute("UPDATE setup_wizard_state SET sample_ticket_key=%s,updated_at=now() WHERE id=true", (key,))


def audit_sample(connection, actor: str, accepted: bool, result: str, project: str, key: str = "") -> None:
    connection.execute("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
                       " VALUES ('setup','sample_ticket','',%s,%s,%s,%s)",
                       (actor, accepted, result[:300], J({"project": project, "ticket": key})))


def record_sample_confirmed(connection, key: str, project: str, actor: str) -> None:
    """Remember that Jira confirmed Intake and mirror the ticket, so later flow moves never reopen the step."""
    connection.execute("UPDATE setup_wizard_state SET sample_confirmed_at=now(),updated_at=now() WHERE id=true"
                       " AND sample_confirmed_at IS NULL")
    connection.execute("INSERT INTO tickets (key,project,status,type,labels,summary,description,jira_created,jira_updated)"
                       " VALUES (%s,%s,'Intake','Task',%s,%s,%s,now(),now())"
                       " ON CONFLICT (key) DO UPDATE SET status='Intake',synced_at=now()",
                       (key, project, ["aiflow-setup-sample"], SAMPLE_SUMMARY, SAMPLE_BODY))
    audit_sample(connection, actor, True, "sample ticket confirmed in Intake", project, key)
