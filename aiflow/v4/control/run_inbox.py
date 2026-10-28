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
                    update_sql: str, params: tuple = (), *, require_running: bool = False,
                    allowed_statuses: tuple[str, ...] = (), require_unrecorded: bool = False) -> tuple[list[int], str]:
    with ctx.db.pool.connection() as connection:
        current = connection.execute("SELECT status,result_received_at,inbox_pending_count FROM runs"
                                     " WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
        if require_running and (not current or current["status"] != "running"):
            raise HTTPException(409, "run is no longer active")
        if allowed_statuses and (not current or current["status"] not in allowed_statuses):
            raise HTTPException(409, "run is no longer active")
        if require_unrecorded and current["result_received_at"]:
            raise HTTPException(409, "run result was already recorded")
        message_ids = (run_inbox.mark_undelivered(connection, run_id, reason)
                       if int(current["inbox_pending_count"] or 0) else [])
        connection.execute(update_sql, params)
    _emit_undelivered(ctx, run, run_id, fence, node, reason, message_ids)
    return message_ids, current["status"]


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
        updated = connection.execute("UPDATE runs SET heartbeat_at=now(), progress=progress || %s"
                                     " WHERE id=%s AND status='running'",
                                     (J(body.progress), run_id)).rowcount
        if not updated:
            raise HTTPException(409, "run is no longer active")
        received = run_inbox.mark_received(connection, run_id, body.inbox_received) if body.inbox_received else []
        included = run_inbox.mark_included(connection, run_id, body.inbox_included) if body.inbox_included else []
        messages = run_inbox.queued(connection, run_id) if int(run.get("inbox_pending_count") or 0) else []
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


def finish(ctx, run: dict, run_id: str, body, details: dict, node: str) -> bool:
    reason = "run ended before the next safe checkpoint"
    update_sql = (
        "UPDATE runs SET outcome=CASE WHEN status='running' THEN %s ELSE outcome END,"
        " error_kind=CASE WHEN status='running' THEN %s ELSE error_kind END,"
        " reset_at=CASE WHEN status='running' THEN %s ELSE reset_at END,"
        " tokens_in=tokens_in+%s,tokens_out=tokens_out+%s,cost_usd=cost_usd+%s,"
        " branch=CASE WHEN status='running' THEN coalesce(nullif(%s,''),branch) ELSE branch END,"
        " pr_url=CASE WHEN status='running' THEN coalesce(nullif(%s,''),pr_url) ELSE pr_url END,"
        " head_sha=CASE WHEN status='running' THEN coalesce(nullif(%s,''),head_sha) ELSE head_sha END,"
        " summary=CASE WHEN status='running' THEN %s ELSE summary END,"
        " details=CASE WHEN status='running' THEN details || %s ELSE details END,"
        " status=CASE WHEN status='running' THEN 'finished' ELSE status END,"
        " ended_at=CASE WHEN status='running' THEN now() ELSE ended_at END,result_received_at=now()"
        " WHERE id=%s AND status IN ('running','lost') AND result_received_at IS NULL")
    params = (body.outcome, body.error_kind, body.reset_at, body.tokens_in, body.tokens_out, body.cost_usd,
              body.branch, body.pr_url, body.head_sha, body.summary[:2000], J(details), run_id)
    _message_ids, run_status = _close_messages(ctx, run, run_id, body.fence, node, reason, update_sql, params,
                                               allowed_statuses=("running", "lost"), require_unrecorded=True)
    late = run_status == "lost"
    return late


def account_update_allowed(ctx, run_id: str, account_id: str, still_owner: bool) -> bool:
    if not still_owner:
        return False
    return not ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' AND id<>%s LIMIT 1",
                          (account_id, run_id))


def watchdog_lost(ctx, run: dict) -> tuple[bool, list[int]]:
    run_id, fence = str(run["id"]), int(run["fence"])
    reason = "run lost before the next safe checkpoint"
    try:
        message_ids, _ = _close_messages(ctx, run, run_id, fence, run["node"], reason,
                                         "UPDATE runs SET status='lost', outcome='lost', ended_at=now()"
                                         " WHERE id=%s AND status='running'", (run["id"],), require_running=True)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return False, []
    return True, message_ids


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
