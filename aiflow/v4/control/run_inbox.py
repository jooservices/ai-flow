"""Run-inbox lifecycle operations shared by worker and console routes."""
from __future__ import annotations

from fastapi import HTTPException

from .db import J
from .repositories import run_inbox


def _emit_undelivered(ctx, run: dict, run_id: str, fence: int, node: str, reason: str,
                      message_ids: list[int]) -> None:
    if message_ids:
        ctx.events.emit("run.inbox.undelivered", ticket=run["ticket_key"], project=run["project"],
                        run_id=run_id, fence=fence, node=node, reason=reason,
                        data={"message_ids": message_ids})


def _close_messages(ctx, run: dict, run_id: str, fence: int, node: str, reason: str,
                    update_sql: str, params: tuple = (), *, require_running: bool = False) -> list[int]:
    with ctx.db.pool.connection() as connection:
        current = connection.execute("SELECT status FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
        if require_running and (not current or current["status"] != "running"):
            raise HTTPException(409, "run is no longer active")
        message_ids = run_inbox.mark_undelivered(connection, run_id, reason)
        connection.execute(update_sql, params)
    _emit_undelivered(ctx, run, run_id, fence, node, reason, message_ids)
    return message_ids


def heartbeat(ctx, run: dict, run_id: str, body, node: str) -> dict:
    if run["node"] != node:
        raise HTTPException(403, "run belongs to another node")
    if run["status"] != "running":
        raise HTTPException(409, "run is no longer active")
    valid = int(run["fence"]) == body.fence and ctx.leases.renew(
        run["ticket_key"], run_id, body.fence, ctx.settings.lease_ms)
    if not valid:
        _close_messages(ctx, run, run_id, body.fence, node, "run lost before the next safe checkpoint",
                        "UPDATE runs SET status='lost', outcome='lost',ended_at=now()"
                        " WHERE id=%s AND status='running'", (run_id,))
        ctx.events.emit("run.lost", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                        account=run["account_id"], node=node, reason="lease expired or taken")
        raise HTTPException(409, "lease lost")

    with ctx.db.pool.connection() as connection:
        current = connection.execute("SELECT status FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
        if not current or current["status"] != "running":
            raise HTTPException(409, "run is no longer active")
        received = run_inbox.mark_received(connection, run_id, body.inbox_received)
        included = run_inbox.mark_included(connection, run_id, body.inbox_included)
        connection.execute("UPDATE runs SET heartbeat_at=now(), progress=progress || %s WHERE id=%s",
                           (J(body.progress), run_id))
        messages = run_inbox.queued(connection, run_id)
    if received:
        ctx.events.emit("run.inbox.received", ticket=run["ticket_key"], project=run["project"],
                        run_id=run_id, fence=body.fence, node=node, outcome="received",
                        data={"message_ids": received})
    if included:
        ctx.events.emit("run.inbox.included", ticket=run["ticket_key"], project=run["project"],
                        run_id=run_id, fence=body.fence, node=node, outcome="included",
                        data={"message_ids": included})
    return {"ok": True, "lease_ms": ctx.settings.lease_ms,
            "inbox": [{**message, "id": int(message["id"]),
                       "created_at": message["created_at"].isoformat(),
                       "received_at": message["received_at"].isoformat() if message["received_at"] else None,
                       "included_at": None} for message in messages]}


def finish(ctx, run: dict, run_id: str, body, details: dict, node: str) -> None:
    reason = "run ended before the next safe checkpoint"
    with ctx.db.pool.connection() as connection:
        current = connection.execute("SELECT status FROM runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
        if not current or current["status"] != "running":
            raise HTTPException(409, "run is no longer active")
        message_ids = run_inbox.mark_undelivered(connection, run_id, reason)
        connection.execute(
            "UPDATE runs SET outcome=%s, error_kind=%s, reset_at=%s, tokens_in=tokens_in+%s, tokens_out=tokens_out+%s,"
            " cost_usd=cost_usd+%s, branch=coalesce(nullif(%s,''),branch), pr_url=coalesce(nullif(%s,''),pr_url),"
            " head_sha=coalesce(nullif(%s,''),head_sha), summary=%s, details=details || %s, status='finished', ended_at=now()"
            " WHERE id=%s",
            (body.outcome, body.error_kind, body.reset_at, body.tokens_in, body.tokens_out, body.cost_usd,
             body.branch, body.pr_url, body.head_sha, body.summary[:2000], J(details), run_id))
    _emit_undelivered(ctx, run, run_id, body.fence, node, reason, message_ids)


def release(ctx, run: dict, run_id: str, body, node: str) -> bool:
    released = ctx.leases.release(run["ticket_key"], run_id, body.fence)
    _close_messages(ctx, run, run_id, body.fence, node, "run released before the next safe checkpoint",
                    "UPDATE runs SET status=CASE WHEN status='running' THEN 'released' ELSE status END,"
                    " ended_at=coalesce(ended_at, now()) WHERE id=%s", (run_id,))
    return released


def restart(ctx, run: dict, target_run_id: str, master_run_id: str, reason: str) -> None:
    terminal_reason = "run restarted before the next safe checkpoint"
    _close_messages(ctx, run, target_run_id, int(run["fence"]), run["node"], terminal_reason,
                    "UPDATE runs SET status='lost',outcome='lost',ended_at=now(), details=details || %s"
                    " WHERE id=%s AND status='running'",
                    (J({"master_restart": {"master_run_id": master_run_id, "reason": reason}}), target_run_id))
