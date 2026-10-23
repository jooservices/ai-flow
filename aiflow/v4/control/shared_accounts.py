"""Shared AI accounts: an account exists apart from machines and can be enabled on several of them.

Design (docs/v4/shared-accounts.md):
- Accounts come from aiflow-control (`accounts.yaml`, one primary machine) or are created in Command Center
  (`shared_accounts`). `account_machines` lists the extra machines an account is enabled on. `overlay` merges both
  into the `ControlRepo` every caller already uses, so `repo.account(id).machines` is the one answer.
- The account has ONE total session limit (`Account.sessions`). Running sessions, quota and cooldown are counted per
  account id across machines. `claim_run` checks the total and per-machine share under a per-account advisory lock,
  so simultaneous workers cannot exceed the limit or take another online machine's share.
- Work is spread: a machine takes at most its fair share (limit / online machines) while other machines are online;
  when one goes offline the share of the others grows and its work moves.
- Credentials stay on the machines that signed in. Control stores only the sign-in prompt a machine reports (a
  provider link and a short-lived device code) and whether the machine signed in.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.controlrepo import Account, ControlRepo
from .db import J

NODE_ONLINE_S = 75
ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
PROVIDER = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{3,63}$")


def overlay(repo: ControlRepo, db) -> ControlRepo:
    """The control repo plus the accounts and machines managed in Command Center (a copy; the snapshot is shared)."""
    try:
        rows = {r["id"]: r for r in db.all("SELECT * FROM shared_accounts")}
        machines: dict[str, list[str]] = {}
        for m in db.all("SELECT account_id, node FROM account_machines ORDER BY enabled_at, node"):
            machines.setdefault(m["account_id"], []).append(m["node"])
    except Exception:  # noqa: BLE001 - before the migration ran, the control repo alone is the truth
        return repo
    if not rows and not machines:
        return repo
    accounts: list[Account] = []
    for a in repo.accounts:
        row = rows.get(a.id)
        accounts.append(replace(a, session_limit=int(row["session_limit"]) if row else a.session_limit,
                                extra_machines=list(a.extra_machines) + machines.get(a.id, [])))
    known = {a.id for a in repo.accounts}
    for row in rows.values():
        if row["managed"] and row["id"] not in known:
            accounts.append(Account(id=row["id"], provider=row["provider"], node="", tier=row["tier"],
                                    roles=list(row["roles"] or []), explicit_only=row["explicit_only"],
                                    session_limit=int(row["session_limit"]), extra_machines=machines.get(row["id"], [])))
    return replace(repo, accounts=accounts)


def online_nodes(db) -> set[str]:
    return {r["name"] for r in db.all(
        "SELECT name FROM nodes WHERE removed_at IS NULL AND status='online' AND last_heartbeat IS NOT NULL"
        " AND last_heartbeat > now() - make_interval(secs => %s)", (NODE_ONLINE_S,))}


def pending_nodes(db, account_id: str) -> set[str]:
    return {r["node"] for r in db.all("SELECT node FROM account_machines WHERE account_id=%s AND status='pending'",
                                      (account_id,))}


def running_by_node(db, account_id: str) -> dict[str, int]:
    return {r["node"]: int(r["n"]) for r in db.all(
        "SELECT node, count(*) AS n FROM runs WHERE account_id=%s AND status='running' GROUP BY node", (account_id,))}


def available_machines(db, acc: Account, node: str) -> list[str]:
    """Machines of the account that can take work now: signed in and online (the asking machine always counts)."""
    pending = pending_nodes(db, acc.id)
    online = online_nodes(db) | {node}
    return [n for n in acc.machines if n not in pending and n in online]


def may_take(db, acc: Account, node: str) -> tuple[bool, str]:
    """Whether `node` may start another session of the account: the total limit and this machine's fair share."""
    if node in pending_nodes(db, acc.id):
        return False, "this machine has not signed in to the account yet"
    running = running_by_node(db, acc.id)
    if acc.session_limit and sum(running.values()) >= acc.sessions:
        return False, f"account is at its limit of {acc.sessions} concurrent sessions"
    machines = available_machines(db, acc, node)
    if acc.session_limit and len(machines) > 1 and running.get(node, 0) >= math.ceil(acc.sessions / len(machines)):
        return False, "other machines of the account have free capacity"
    return True, ""


def claim_run(db, acc: Account, values: tuple) -> bool:
    """Atomically enforce the account total and machine fair share before inserting a running session."""
    node = str(values[7])
    if not acc.runs_on(node):
        return False
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + acc.id,))
            if acc.session_limit:
                pending = {row["node"] for row in conn.execute(
                    "SELECT node FROM account_machines WHERE account_id=%s AND status='pending'", (acc.id,))}
                if node in pending:
                    return False
                counts = {row["node"]: int(row["n"]) for row in conn.execute(
                    "SELECT node,count(*) AS n FROM runs WHERE account_id=%s AND status='running' GROUP BY node",
                    (acc.id,))}
                if sum(counts.values()) >= acc.sessions:
                    return False
                online = {row["name"] for row in conn.execute(
                    "SELECT name FROM nodes WHERE removed_at IS NULL AND status='online' AND last_heartbeat IS NOT NULL"
                    " AND last_heartbeat > now() - make_interval(secs => %s)", (NODE_ONLINE_S,))}
                available = [machine for machine in acc.machines
                             if machine not in pending and (machine == node or machine in online)]
                if len(available) > 1 and counts.get(node, 0) >= math.ceil(acc.sessions / len(available)):
                    return False
            conn.execute("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, model, node, fence,"
                         " token_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", values)
    return True


def mark_signed_in(db, account_id: str, node: str) -> None:
    db.run("UPDATE account_machines SET status='signed_in', signed_in_at=coalesce(signed_in_at, now()),"
           " signin_link='', signin_code='', signin_expires_at=NULL WHERE account_id=%s AND node=%s",
           (account_id, node))


def machines_view(db, acc: Account | None, node_status: dict[str, str], *,
                  include_signin: bool = False) -> list[dict[str, Any]]:
    """Per-machine breakdown of one account: where it is enabled, whether signed in, what runs there."""
    if not acc:
        return []
    rows = {r["node"]: r for r in db.all("SELECT * FROM account_machines WHERE account_id=%s", (acc.id,))}
    running = running_by_node(db, acc.id)
    recent = {r["node"]: int(r["n"]) for r in db.all(
        "SELECT node, count(*) AS n FROM runs WHERE account_id=%s AND started_at > now() - interval '24 hours'"
        " GROUP BY node", (acc.id,))}
    now = datetime.now(timezone.utc)
    out = []
    for n in acc.machines:
        row = rows.get(n)
        pending = bool(row and row["status"] == "pending")
        prompt = None
        if (include_signin and pending and row["signin_link"] and row["signin_expires_at"]
                and row["signin_expires_at"] > now):
            prompt = {"link": row["signin_link"], "code": row["signin_code"],
                      "expires_at": row["signin_expires_at"].isoformat()}
        out.append({"node": n, "node_status": node_status.get(n, "offline"), "signed_in": not pending,
                    "removable": bool(row), "runs_active": running.get(n, 0), "runs_24h": recent.get(n, 0),
                    "state": ((row or {}).get("worker_state") or {}).get("state", ""), "signin": prompt,
                    "credential_expires_at": (row["credential_expires_at"].isoformat()
                                              if row and row["credential_expires_at"] else None)})
    return out


# --- request bodies ---------------------------------------------------------------------------------------
class AccountBody(BaseModel):
    id: str = Field(min_length=2, max_length=40)
    provider: str = Field(min_length=2, max_length=24)
    tier: str = Field(default="middle", pattern=r"^(junior|middle|senior)$")
    roles: list[str] = Field(default_factory=lambda: ["dev", "verify"], max_length=10)
    explicit_only: bool = False
    session_limit: int = Field(default=1, ge=1, le=50)


class LimitBody(BaseModel):
    session_limit: int = Field(ge=1, le=50)


class MachineBody(BaseModel):
    node: str = Field(min_length=1, max_length=63)


class SigninPromptBody(BaseModel):
    link: str = Field(max_length=500)
    code: str = Field(max_length=64)
    expires_in_s: int = Field(default=900, ge=60, le=3600)


def install(app: FastAPI, ctx, console_user, audit, node_auth) -> None:
    sync_accounts = app.state.sync_accounts

    def sync() -> list[str]:
        warnings = sync_accounts()
        for row in ctx.db.all("SELECT id,provider,tier,roles FROM shared_accounts WHERE managed"):
            ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
                       " VALUES (%s,%s,'',%s,%s,'signed_out') ON CONFLICT (id) DO NOTHING",
                       (row["id"], row["provider"], row["tier"], row["roles"] or []))
        return warnings

    app.state.sync_accounts = sync

    def account_or_404(account_id: str) -> Account:
        acc = ctx.repo().account(account_id)
        if not acc:
            raise HTTPException(404, f"unknown account {account_id}")
        return acc

    def view(acc: Account, *, include_signin: bool = False) -> dict[str, Any]:
        status = {r["name"]: "online" if r["name"] in online_nodes(ctx.db) else "offline"
                  for r in ctx.db.all("SELECT name FROM nodes")}
        running = sum(running_by_node(ctx.db, acc.id).values())
        return {"id": acc.id, "provider": acc.provider, "tier": acc.tier, "roles": acc.roles,
                "session_limit": acc.sessions, "sessions_active": running,
                "machines": machines_view(ctx.db, acc, status, include_signin=include_signin)}

    @app.get("/v1/shared-accounts")
    def list_accounts(request: Request) -> dict:
        user = console_user(request, {"console.read"})
        include_signin = "nodes.manage" in user.permissions or "*" in user.permissions
        return {"accounts": [view(a, include_signin=include_signin) for a in ctx.repo().accounts]}

    @app.post("/v1/shared-accounts", status_code=201)
    def create_account(body: AccountBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="create", node=body.id)
        if not ACCOUNT_ID.match(body.id) or not PROVIDER.match(body.provider):
            raise HTTPException(422, "account id and provider use lowercase letters, digits and dashes")
        if ctx.repo().account(body.id):
            raise HTTPException(409, f"account {body.id} already exists")
        ctx.db.run("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit, created_by)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                   (body.id, body.provider, body.tier, body.roles, body.explicit_only, body.session_limit,
                    user.subject))
        sync()
        audit("account", "create", body.id, "", None, user.subject, True, "created")
        return view(account_or_404(body.id), include_signin=True)

    @app.put("/v1/shared-accounts/{account_id}/session-limit")
    def set_limit(account_id: str, body: LimitBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="session_limit", node=account_id)
        acc = account_or_404(account_id)
        ctx.db.run("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit, managed,"
                   " created_by) VALUES (%s,%s,%s,%s,%s,%s,false,%s)"
                   " ON CONFLICT (id) DO UPDATE SET session_limit=EXCLUDED.session_limit",
                   (acc.id, acc.provider, acc.tier, acc.roles, acc.explicit_only, body.session_limit, user.subject))
        audit("account", "session_limit", acc.id, "", None, user.subject, True, str(body.session_limit))
        return view(account_or_404(account_id), include_signin=True)

    @app.post("/v1/shared-accounts/{account_id}/machines", status_code=201)
    def enable_machine(account_id: str, body: MachineBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="enable_machine", node=account_id)
        acc = account_or_404(account_id)
        if not ctx.db.one("SELECT 1 AS x FROM nodes WHERE name=%s", (body.node,)):
            raise HTTPException(404, f"machine {body.node} has not joined")
        if acc.runs_on(body.node):
            raise HTTPException(409, f"{acc.id} is already enabled on {body.node}")
        # an account on several machines always has one total limit: keep what the account had per machine
        ctx.db.run("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit, managed,"
                   " created_by) VALUES (%s,%s,%s,%s,%s,%s,false,%s) ON CONFLICT (id) DO NOTHING",
                   (acc.id, acc.provider, acc.tier, acc.roles, acc.explicit_only, acc.sessions, user.subject))
        ctx.db.run("INSERT INTO account_machines (account_id, node, enabled_by) VALUES (%s,%s,%s)",
                   (acc.id, body.node, user.subject))
        sync()
        audit("account", "enable_machine", acc.id, "", None, user.subject, True, body.node)
        ctx.events.emit("account.machine_enabled", account=acc.id, node=body.node, outcome="pending")
        return view(account_or_404(account_id), include_signin=True)

    @app.delete("/v1/shared-accounts/{account_id}/machines/{node}")
    def remove_machine(account_id: str, node: str, request: Request, confirm: bool = False) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="remove_machine", node=account_id)
        acc = account_or_404(account_id)
        if not acc.runs_on(node):
            raise HTTPException(404, f"{acc.id} is not enabled on {node}")
        if not ctx.db.one("SELECT 1 AS x FROM account_machines WHERE account_id=%s AND node=%s", (acc.id, node)):
            raise HTTPException(409, f"{acc.id} is assigned to {node} in aiflow-control/accounts.yaml")
        last = len(acc.machines) == 1
        if last and not confirm:
            raise HTTPException(409, {"error": "confirmation_required",
                                      "message": f"{node} is the last machine of {acc.id}; removing it removes the "
                                                 "account from every machine. Confirm to continue."})
        ctx.db.run("DELETE FROM account_machines WHERE account_id=%s AND node=%s", (acc.id, node))
        removed_account = False
        if last:
            removed_account = delete_account(acc)
        audit("account", "remove_machine", acc.id, "", None, user.subject, True, node,
              {"account_removed": removed_account})
        ctx.events.emit("account.machine_removed", account=acc.id, node=node, outcome="removed")
        return {"account": acc.id, "node": node, "account_removed": removed_account,
                "machines": [] if removed_account else [n for n in acc.machines if n != node]}

    def delete_account(acc: Account) -> bool:
        row = ctx.db.one("SELECT managed FROM shared_accounts WHERE id=%s", (acc.id,))
        if not row or not row["managed"]:
            return False
        ctx.db.run("DELETE FROM shared_accounts WHERE id=%s", (acc.id,))
        ctx.db.run("DELETE FROM account_machines WHERE account_id=%s", (acc.id,))
        ctx.db.run("DELETE FROM accounts WHERE id=%s", (acc.id,))
        return True

    @app.delete("/v1/shared-accounts/{account_id}")
    def remove_account(account_id: str, request: Request, confirm: bool = False) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="remove", node=account_id)
        acc = account_or_404(account_id)
        row = ctx.db.one("SELECT managed FROM shared_accounts WHERE id=%s", (acc.id,))
        if not row or not row["managed"]:
            raise HTTPException(409, f"{acc.id} is defined in aiflow-control/accounts.yaml")
        if not confirm:
            raise HTTPException(409, {"error": "confirmation_required",
                                      "message": f"Removing {acc.id} removes it from all {len(acc.machines)} "
                                                 "machines. Confirm to continue."})
        delete_account(acc)
        audit("account", "remove", acc.id, "", None, user.subject, True, "removed from all machines")
        ctx.events.emit("account.removed", account=acc.id, outcome="removed")
        return {"account": acc.id, "removed": True, "machines": list(acc.machines)}

    @app.post("/v1/accounts/{account_id}/signin-prompt")
    def signin_prompt(account_id: str, body: SigninPromptBody, node: str = Depends(node_auth)) -> dict:
        """A machine reports the provider's sign-in link and device code so Command Center can show them. The
        credential itself is created by the provider CLI on the machine and never sent here."""
        acc = ctx.repo().account(account_id)
        if not acc or not acc.runs_on(node):
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        parsed = urlparse(body.link)
        if parsed.scheme != "https" or not parsed.netloc or not CODE.match(body.code):
            raise HTTPException(422, "sign-in link must be https and the code a short device code")
        changed = ctx.db.run(
            "UPDATE account_machines SET signin_link=%s, signin_code=%s,"
            " signin_expires_at=now() + make_interval(secs => %s) WHERE account_id=%s AND node=%s AND status='pending'",
            (body.link, body.code, body.expires_in_s, acc.id, node))
        return {"stored": bool(changed),
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=body.expires_in_s)).isoformat()}
