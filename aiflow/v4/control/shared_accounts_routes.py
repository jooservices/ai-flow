"""Request work for the shared account routes; route handlers keep event publication visible."""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException


def sync_managed_accounts(ctx, sync_accounts) -> list[str]:
    warnings = sync_accounts()
    for row in ctx.db.all("SELECT id,provider,tier,roles FROM shared_accounts WHERE managed"):
        ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
                   " VALUES (%s,%s,'',%s,%s,'signed_out') ON CONFLICT (id) DO NOTHING",
                   (row["id"], row["provider"], row["tier"], row["roles"] or []))
    return warnings


def account_or_404(ctx, account_id: str):
    account = ctx.repo().account(account_id)
    if not account:
        raise HTTPException(404, f"unknown account {account_id}")
    return account


def account_view(ctx, account, *, include_signin: bool, online_nodes_fn, running_by_node_fn,
                 machines_view_fn) -> dict[str, Any]:
    status = {row["name"]: "online" if row["name"] in online_nodes_fn(ctx.db) else "offline"
              for row in ctx.db.all("SELECT name FROM nodes")}
    running = sum(running_by_node_fn(ctx.db, account.id).values())
    return {"id": account.id, "provider": account.provider, "tier": account.tier, "roles": account.roles,
            "session_limit": account.sessions, "sessions_active": running,
            "machines": machines_view_fn(ctx.db, account, status, include_signin=include_signin)}


def create_account_record(ctx, body, user, sync_accounts, account_id_pattern, provider_pattern):
    if not account_id_pattern.match(body.id) or not provider_pattern.match(body.provider):
        raise HTTPException(422, "account id and provider use lowercase letters, digits and dashes")
    if ctx.repo().account(body.id):
        raise HTTPException(409, f"account {body.id} already exists")
    ctx.db.run("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit, created_by)"
               " VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (body.id, body.provider, body.tier, body.roles, body.explicit_only, body.session_limit, user.subject))
    sync_accounts()


def set_session_limit_record(ctx, account_id: str, body, user):
    account = account_or_404(ctx, account_id)
    with ctx.db.pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + account.id,))
        active = int(conn.execute("SELECT count(*) AS n FROM runs WHERE account_id=%s AND status='running'",
                                  (account.id,)).fetchone()["n"])
        if body.session_limit < active:
            raise HTTPException(409, {"error": "limit_below_active_sessions",
                                      "message": f"{account.id} has {active} running sessions; wait for "
                                                 f"{active - body.session_limit} to finish before lowering "
                                                 f"the limit to {body.session_limit}."})
        conn.execute("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit,"
                     " managed, created_by) VALUES (%s,%s,%s,%s,%s,%s,false,%s)"
                     " ON CONFLICT (id) DO UPDATE SET session_limit=EXCLUDED.session_limit",
                     (account.id, account.provider, account.tier, account.roles, account.explicit_only,
                      body.session_limit, user.subject))
    return account


def enable_machine_record(ctx, account_id: str, body, user, sync_accounts):
    account = account_or_404(ctx, account_id)
    if not ctx.db.one("SELECT 1 AS x FROM nodes WHERE name=%s", (body.node,)):
        raise HTTPException(404, f"machine {body.node} has not joined")
    if account.runs_on(body.node):
        raise HTTPException(409, f"{account.id} is already enabled on {body.node}")
    ctx.db.run("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit, managed,"
               " created_by) VALUES (%s,%s,%s,%s,%s,%s,false,%s) ON CONFLICT (id) DO NOTHING",
               (account.id, account.provider, account.tier, account.roles, account.explicit_only, account.sessions,
                user.subject))
    with ctx.db.pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + account.id,))
        conn.execute("DELETE FROM account_machine_exclusions WHERE account_id=%s AND node=%s",
                     (account.id, body.node))
        conn.execute("INSERT INTO account_machines (account_id, node, enabled_by) VALUES (%s,%s,%s)",
                     (account.id, body.node, user.subject))
    sync_accounts()
    return account


def retry_signin_record(ctx, account_id: str, node: str, user):
    account = account_or_404(ctx, account_id)
    changed = ctx.db.run(
        "UPDATE account_machines SET signin_attempt=signin_attempt + 1, signin_link='', signin_code='',"
        " signin_expires_at=NULL WHERE account_id=%s AND node=%s AND status='pending'", (account.id, node))
    if not changed:
        raise HTTPException(409, f"{account.id} has no pending sign-in on {node}")
    return account


def detach_machine(ctx, account, node: str, actor: str) -> None:
    with ctx.db.pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + account.id,))
        conn.execute("DELETE FROM account_machines WHERE account_id=%s AND node=%s", (account.id, node))
        conn.execute("INSERT INTO account_machine_exclusions (account_id,node,detached_by) VALUES (%s,%s,%s)"
                     " ON CONFLICT (account_id,node) DO UPDATE SET detached_by=EXCLUDED.detached_by,"
                     " detached_at=now()", (account.id, node, actor))


def delete_account(ctx, account) -> bool:
    with ctx.db.pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + account.id,))
        row = conn.execute("SELECT managed FROM shared_accounts WHERE id=%s FOR UPDATE",
                           (account.id,)).fetchone()
        if not row or not row["managed"]:
            return False
        conn.execute("DELETE FROM account_machines WHERE account_id=%s", (account.id,))
        conn.execute("DELETE FROM account_machine_exclusions WHERE account_id=%s", (account.id,))
        conn.execute("DELETE FROM shared_accounts WHERE id=%s", (account.id,))
        conn.execute("DELETE FROM accounts WHERE id=%s", (account.id,))
    return True


def remove_machine_record(ctx, account_id: str, node: str, user, confirm: bool):
    account = account_or_404(ctx, account_id)
    if not account.runs_on(node):
        raise HTTPException(404, f"{account.id} is not enabled on {node}")
    if ctx.db.one("SELECT 1 AS x FROM managed_ai_accounts WHERE id=%s AND removed_at IS NULL", (account.id,)):
        raise HTTPException(409, f"{account.id} is managed from the machine account screen")
    last = len(account.machines) == 1
    if last and not confirm:
        raise HTTPException(409, {"error": "confirmation_required",
                                  "message": f"{node} is the last machine of {account.id}; removing it removes the "
                                             "account from every machine. Confirm to continue."})
    detach_machine(ctx, account, node, user.subject)
    removed_account = delete_account(ctx, account) if last else False
    return account, removed_account


def remove_account_record(ctx, account_id: str, user, confirm: bool):
    account = account_or_404(ctx, account_id)
    if ctx.db.one("SELECT 1 AS x FROM managed_ai_accounts WHERE id=%s AND removed_at IS NULL", (account.id,)):
        raise HTTPException(409, f"{account.id} is managed from the machine account screen")
    row = ctx.db.one("SELECT managed FROM shared_accounts WHERE id=%s", (account.id,))
    if not confirm:
        raise HTTPException(409, {"error": "confirmation_required",
                                  "message": f"Removing {account.id} removes it from all {len(account.machines)} "
                                             "machines. Confirm to continue."})
    if row and row["managed"]:
        delete_account(ctx, account)
        retained = False
    else:
        for node in account.machines:
            detach_machine(ctx, account, node, user.subject)
        retained = True
    return account, retained


def signin_prompt_record(ctx, account_id: str, body, node: str, code_pattern, datetime_type):
    account = ctx.repo().account(account_id)
    if not account or not account.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    parsed = urlparse(body.link)
    if parsed.scheme != "https" or not parsed.netloc or not code_pattern.match(body.code):
        raise HTTPException(422, "sign-in link must be https and the code a short device code")
    changed = ctx.db.run(
        "UPDATE account_machines SET signin_link=%s, signin_code=%s,"
        " signin_expires_at=now() + make_interval(secs => %s) WHERE account_id=%s AND node=%s AND status='pending'",
        (body.link, body.code, body.expires_in_s, account.id, node))
    return {"stored": bool(changed),
            "expires_at": (datetime_type.now(timezone.utc) + timedelta(seconds=body.expires_in_s)).isoformat()}


def begin_signin_complete(ctx, account_id: str, body, node: str):
    account = ctx.repo().account(account_id)
    if not account or not account.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    row = ctx.db.one("SELECT status,signin_attempt FROM account_machines WHERE account_id=%s AND node=%s",
                     (account.id, node))
    if not row:
        raise HTTPException(409, f"{account.id} has no shared sign-in on {node}")
    expected = f"shared:{node}:{account.id}:{row['signin_attempt']}"
    if body.request_id != expected:
        raise HTTPException(409, "shared sign-in attempt is no longer current")
    emit_completed = False
    if row["status"] == "pending":
        changed = ctx.db.run(
            "UPDATE account_machines SET status='signed_in', signed_in_at=now(), signin_link='', signin_code='',"
            " signin_expires_at=NULL WHERE account_id=%s AND node=%s AND status='pending' AND signin_attempt=%s",
            (account.id, node, row["signin_attempt"]))
        if not changed:
            current = ctx.db.one("SELECT status,signin_attempt FROM account_machines"
                                 " WHERE account_id=%s AND node=%s", (account.id, node))
            if not current or current["signin_attempt"] != row["signin_attempt"] or current["status"] != "signed_in":
                raise HTTPException(409, "shared sign-in attempt is no longer current")
        else:
            emit_completed = True
    elif row["status"] != "signed_in":
        raise HTTPException(409, f"{account.id} is not waiting for sign-in on {node}")
    return account, emit_completed


def finish_signin_complete(ctx, account_id: str) -> None:
    ctx.db.run("UPDATE accounts SET last_probe_at=now(),status=CASE WHEN status='signed_out' THEN 'idle'"
               " ELSE status END,last_error=CASE WHEN status='signed_out' THEN '' ELSE last_error END,"
               "updated_at=now() WHERE id=%s", (account_id,))
