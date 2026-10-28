"""Persistence for ordered, idempotent guidance sent to an active worker run."""
from __future__ import annotations

from uuid import UUID


def get_run(db, run_id: str) -> dict | None:
    return db.one("SELECT id,ticket_key,project,node,fence,status,step FROM runs WHERE id=%s", (run_id,))


def get_run_locked(connection, run_id: str) -> dict | None:
    return connection.execute(
        "SELECT id,ticket_key,project,node,fence,status,step FROM runs WHERE id=%s FOR UPDATE", (run_id,)
    ).fetchone()


def create(connection, run: dict, client_id: UUID, author: str, kind: str, body: str) -> tuple[dict, bool]:
    existing = connection.execute(
        "SELECT id,run_id,client_id,ticket_key,project,author,kind,body,status,created_at,received_at,included_at,terminal_reason"
        " FROM run_inbox_messages WHERE run_id=%s AND client_id=%s", (run["id"], client_id)
    ).fetchone()
    if existing:
        return existing, False
    row = connection.execute(
        "INSERT INTO run_inbox_messages (run_id,client_id,ticket_key,project,author,kind,body)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id,run_id,client_id,ticket_key,project,author,kind,body,status,"
        " created_at,received_at,included_at,terminal_reason",
        (run["id"], client_id, run["ticket_key"], run["project"], author, kind, body),
    ).fetchone()
    return row, True


def queued(connection, run_id: str) -> list[dict]:
    return list(connection.execute(
        "SELECT id,client_id,author,kind,body,status,created_at,received_at,included_at,terminal_reason"
        " FROM run_inbox_messages WHERE run_id=%s AND status IN ('queued','received') ORDER BY id LIMIT 10",
        (run_id,)).fetchall())


def latest(db, run_id: str, limit: int = 100) -> list[dict]:
    rows = db.all(
        "SELECT id,run_id,client_id,author,kind,body,status,created_at,received_at,included_at,terminal_reason"
        " FROM run_inbox_messages WHERE run_id=%s ORDER BY id DESC LIMIT %s", (run_id, limit))
    return list(reversed(rows))


def mark_received(connection, run_id: str, message_ids: list[int]) -> list[int]:
    if message_ids:
        rows = connection.execute(
            "UPDATE run_inbox_messages SET status='received',received_at=coalesce(received_at,now())"
            " WHERE run_id=%s AND id=ANY(%s) AND status='queued' RETURNING id", (run_id, message_ids)).fetchall()
        return [int(row["id"]) for row in rows]
    return []


def mark_included(connection, run_id: str, message_ids: list[int]) -> list[int]:
    if message_ids:
        rows = connection.execute(
            "UPDATE run_inbox_messages SET status='included',included_at=coalesce(included_at,now())"
            " WHERE run_id=%s AND id=ANY(%s) AND status IN ('queued','received') RETURNING id",
            (run_id, message_ids)).fetchall()
        return [int(row["id"]) for row in rows]
    return []


def mark_undelivered(connection, run_id: str, reason: str) -> list[int]:
    result = connection.execute(
        "UPDATE run_inbox_messages SET status='undelivered',terminal_reason=%s"
        " WHERE run_id=%s AND status IN ('queued','received') RETURNING id", (reason[:300], run_id))
    return [int(row["id"]) for row in result.fetchall()]
