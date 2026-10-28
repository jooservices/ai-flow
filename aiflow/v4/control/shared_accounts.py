"""Shared AI accounts: an account exists apart from machines and can be enabled on several of them.

Design (docs/v4/shared-accounts.md):
- Accounts come from aiflow-control (`accounts.yaml`, one primary machine) or are created in Command Center
  (`shared_accounts`). `account_machines` lists machines enabled in Command Center, and
  `account_machine_exclusions` records a machine detached from a control-repo account. `overlay` merges these into
  the `ControlRepo` every caller already uses, so `repo.account(id).machines` is the one answer.
- The account has ONE total session limit (`Account.sessions`). Running sessions, quota and cooldown are counted per
  account id across machines. `claim_run` checks the total, per-machine share, free worker slots and concurrent quota
  reservations under account and machine advisory locks, so simultaneous workers cannot exceed capacity.
- Work is spread across online machines in proportion to their free worker slots, capped by each machine's free
  capacity; when a machine goes offline or runs out of capacity the others can take its share.
- Credentials stay on the machines that signed in. Control stores only the sign-in prompt a machine reports (a
  provider link and a short-lived device code) and whether the machine signed in.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .capacity import reported_run_tokens, reservation_tokens
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
        exclusions: dict[str, set[str]] = {}
        for row in db.all("SELECT account_id,node FROM account_machine_exclusions"):
            exclusions.setdefault(row["account_id"], set()).add(row["node"])
    except Exception:  # noqa: BLE001 - before the migration ran, the control repo alone is the truth
        return repo
    if not rows and not machines and not exclusions:
        return repo
    accounts: list[Account] = []
    for a in repo.accounts:
        row = rows.get(a.id)
        detached = exclusions.get(a.id, set())
        accounts.append(replace(a, node="" if a.node in detached else a.node,
                                session_limit=int(row["session_limit"]) if row else a.session_limit,
                                extra_machines=[n for n in list(a.extra_machines) + machines.get(a.id, [])
                                                if n not in detached]))
    known = {a.id for a in repo.accounts}
    for row in rows.values():
        if row["managed"] and row["id"] not in known:
            accounts.append(Account(id=row["id"], provider=row["provider"], node="", tier=row["tier"],
                                    roles=list(row["roles"] or []), explicit_only=row["explicit_only"],
                                    session_limit=int(row["session_limit"]),
                                    extra_machines=[n for n in machines.get(row["id"], [])
                                                    if n not in exclusions.get(row["id"], set())]))
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
    """Machines of the account that are signed in, online, and have a free worker slot."""
    if not acc.runs_on(node):
        return []
    pending = pending_nodes(db, acc.id)
    candidates = [n for n in acc.machines if n not in pending]
    capacities = machine_capacities(db, candidates, acc.id)
    return [n for n in candidates if capacities.get(n, {}).get("free", 0) > 0]


def _all(queryable, sql: str, params: tuple) -> list[dict]:
    """Read through DB's convenience method or the active transaction connection."""
    if hasattr(queryable, "all"):
        return queryable.all(sql, params)
    return list(queryable.execute(sql, params).fetchall())


def machine_capacities(queryable, machines: list[str] | set[str], account_id: str) -> dict[str, dict[str, int]]:
    """Free worker slots and the account's capacity on each currently online machine."""
    names = sorted(set(machines))
    if not names:
        return {}
    rows = _all(
        queryable,
        "SELECT n.name,n.slots_total,count(r.id) FILTER (WHERE r.status='running') AS active_runs,"
        " count(r.id) FILTER (WHERE r.status='running' AND r.account_id<>%s) AS other_runs"
        " FROM nodes n LEFT JOIN runs r ON r.node=n.name"
        " WHERE n.name=ANY(%s) AND n.removed_at IS NULL AND n.status='online' AND n.last_heartbeat IS NOT NULL"
        " AND n.last_heartbeat > now() - make_interval(secs => %s) GROUP BY n.name,n.slots_total",
        (account_id, names, NODE_ONLINE_S))
    out = {}
    for row in rows:
        slots = max(0, int(row["slots_total"] or 0))
        active = int(row["active_runs"] or 0)
        other = int(row["other_runs"] or 0)
        out[row["name"]] = {"free": max(0, slots - active), "account": max(0, slots - other)}
    return out


def capacity_shares(session_limit: int, capacities: dict[str, int]) -> dict[str, int]:
    """Apportion the account limit in whole sessions, proportional to machine capacity.

    Largest-remainder apportionment keeps the integer shares close to their capacity weights while guaranteeing
    that their sum never exceeds the account limit. Ties are stable by machine name.
    """
    weights = {node: max(0, int(capacity)) for node, capacity in capacities.items() if int(capacity) > 0}
    total_capacity = sum(weights.values())
    if session_limit <= 0 or total_capacity <= 0:
        return {}
    allocated = min(int(session_limit), total_capacity)
    shares = {node: allocated * weight // total_capacity for node, weight in weights.items()}
    remaining = allocated - sum(shares.values())
    order = sorted(weights, key=lambda node: (-(allocated * weights[node] % total_capacity), node))
    for node in order[:remaining]:
        shares[node] += 1
    return shares


def may_take(db, acc: Account, node: str, *, as_peer: bool = False) -> tuple[bool, str]:
    """Whether `node` has free worker capacity and may start its capacity-weighted account share."""
    pending = pending_nodes(db, acc.id)
    if node in pending:
        return False, "this machine has not signed in to the account yet"
    if not acc.runs_on(node):
        return False, "this account is not enabled on this machine"
    # Ordinary control-repo accounts have no shared session_limit; their node agent enforces `parallel`.
    # Capacity aggregation only changes admission for shared accounts and quota-routing peers.
    bounded = bool(acc.session_limit or as_peer)
    if not bounded:
        return True, ""
    candidates = [n for n in acc.machines if n not in pending]
    capacities = machine_capacities(db, candidates, acc.id)
    # A configured account without a shared limit is bounded by its `sessions` and machine slots only when it is
    # judged as a quota-routing peer; its own claims keep the worker-level limits they always had.
    if bounded and capacities.get(node, {}).get("free", 0) <= 0:
        return False, "this machine has no free worker slots; its capacity is full"
    running = running_by_node(db, acc.id)
    if bounded and sum(running.values()) >= acc.sessions:
        return False, f"account is at its limit of {acc.sessions} concurrent sessions"
    if acc.session_limit:
        eligible_capacities = {machine: values["account"] for machine, values in capacities.items()
                               if values["free"] > 0}
        share = capacity_shares(acc.sessions, eligible_capacities).get(node, 0)
        if running.get(node, 0) >= share:
            return False, "other machines of the account have free capacity"
    return True, ""


def claim_run(db, acc: Account, values: tuple, *, max_node_runs: int | None = None,
              hostname: str = "", max_host_runs: int | None = None,
              run_details: dict | None = None, quota_reservation_tokens: int | None = None) -> bool:
    """Atomically enforce account, machine, host-load and concurrent quota limits before inserting a run."""
    node = str(values[7])
    if not acc.runs_on(node):
        return False
    with db.pool.connection() as conn:
        with conn.transaction():
            if hostname:
                # Serialize claims by host so workers on separate machines share the same load budget.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("host-load:" + hostname,))
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + acc.id,))
            machines = sorted(set(acc.machines))
            # All claims take machine locks in the same order. Shared claims also use the locked counts to enforce
            # their capacity-weighted share, including work claimed concurrently by a different account.
            for machine in machines:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                             (f"aiflow4-machine-sessions:{machine}",))
            if max_node_runs is not None:
                current = conn.execute("SELECT count(*) AS n FROM runs WHERE node=%s AND status='running'",
                                       (node,)).fetchone()
                if int(current["n"] or 0) >= max_node_runs:
                    return False
            if hostname and max_host_runs is not None:
                current_host = conn.execute(
                    "SELECT count(*) AS n FROM runs r JOIN nodes n ON n.name=r.node"
                    " WHERE r.status='running' AND lower(n.metrics->>'hostname')=%s", (hostname,)).fetchone()
                if int(current_host["n"] or 0) >= max_host_runs:
                    return False
            # the limit is read under the lock, so a concurrent change is never missed
            stored = conn.execute("SELECT session_limit FROM shared_accounts WHERE id=%s", (acc.id,)).fetchone()
            if stored:
                acc = replace(acc, session_limit=int(stored["session_limit"]))
            machine_account = conn.execute("SELECT node,removed_at FROM managed_ai_accounts WHERE id=%s",
                                            (acc.id,)).fetchone()
            if machine_account:
                assigned = machine_account["removed_at"] is None and machine_account["node"] == node
            else:
                configured_assignment = node == acc.node and not conn.execute(
                    "SELECT 1 FROM account_machine_exclusions WHERE account_id=%s AND node=%s",
                    (acc.id, node)).fetchone()
                enabled_assignment = conn.execute(
                    "SELECT 1 FROM account_machines WHERE account_id=%s AND node=%s", (acc.id, node)).fetchone()
                assigned = bool(configured_assignment or enabled_assignment)
            if not assigned:
                return False
            pending = {row["node"] for row in conn.execute(
                "SELECT node FROM account_machines WHERE account_id=%s AND status='pending'", (acc.id,))}
            if node in pending:
                return False
            counts = {row["node"]: int(row["n"]) for row in conn.execute(
                "SELECT node,count(*) AS n FROM runs WHERE account_id=%s AND status='running' GROUP BY node",
                (acc.id,))}
            if acc.session_limit:
                capacities = machine_capacities(conn, [m for m in machines if m not in pending], acc.id)
                if capacities.get(node, {}).get("free", 0) <= 0:
                    return False
                if sum(counts.values()) >= acc.sessions:
                    return False
                eligible_capacities = {machine: values["account"] for machine, values in capacities.items()
                                       if values["free"] > 0}
                share = capacity_shares(acc.sessions, eligible_capacities).get(node, 0)
                if counts.get(node, 0) >= share:
                    return False
            if quota_reservation_tokens and quota_reservation_tokens > 0:
                account = conn.execute("SELECT quota_forecast,quota_observed_at FROM accounts WHERE id=%s",
                                       (acc.id,)).fetchone()
                forecast = (account or {}).get("quota_forecast") or {}
                windows = forecast.get("windows") if isinstance(forecast, dict) else []
                balances = [int(window["left_tokens"]) for window in windows or []
                            if isinstance(window, dict) and window.get("left_tokens") is not None]
                if balances:
                    reserved = 0
                    for active in conn.execute("SELECT details FROM runs WHERE account_id=%s AND status='running'",
                                               (acc.id,)):
                        detail = active.get("details") if isinstance(active.get("details"), dict) else {}
                        reserved += reservation_tokens(detail)
                    reported = 0
                    observed_at = (account or {}).get("quota_observed_at")
                    if forecast.get("source") == "reported" and observed_at:
                        for completed in conn.execute(
                                "SELECT details,tokens_in,tokens_out FROM runs WHERE account_id=%s"
                                " AND status='finished' AND ended_at>%s", (acc.id, observed_at)):
                            reported += reported_run_tokens(completed)
                    reserved += reported
                    if reserved and min(balances) - reserved < quota_reservation_tokens:
                        return False
            else:
                quota_reservation_tokens = None
            details = dict(run_details or {})
            if quota_reservation_tokens:
                details["quota_reservation"] = {"tokens": int(quota_reservation_tokens)}
            conn.execute("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, model, node, fence,"
                         " token_hash,details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*values, J(details)))
    return True


def mark_pending(db, acc: Account, node: str) -> bool:
    """A machine lost its sign-in: only that machine goes back to pending (and gets a new sign-in request).
    Returns True when another machine of the account is still signed in."""
    db.run("INSERT INTO account_machines (account_id, node, status) VALUES (%s,%s,'pending')"
           " ON CONFLICT (account_id, node) DO UPDATE SET status='pending', signed_in_at=NULL,"
           " signin_attempt=account_machines.signin_attempt + 1, signin_link='', signin_code='', signin_expires_at=NULL", (acc.id, node))
    pending = pending_nodes(db, acc.id)
    return any(m not in pending for m in acc.machines)


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
    machine_managed = bool(db.one("SELECT 1 AS x FROM managed_ai_accounts WHERE id=%s AND removed_at IS NULL",
                                  (acc.id,)))
    recent = {r["node"]: int(r["n"]) for r in db.all(
        "SELECT node, count(*) AS n FROM runs WHERE account_id=%s AND started_at > now() - interval '24 hours'"
        " GROUP BY node", (acc.id,))}
    usage_today = {r["node"]: r for r in db.all(
        "SELECT node, coalesce(sum(tokens_in + tokens_out),0) AS tokens, coalesce(sum(cost_usd),0) AS cost"
        " FROM runs WHERE account_id=%s AND started_at >= date_trunc('day',now()) GROUP BY node", (acc.id,))}
    now = datetime.now(timezone.utc)
    out = []
    for n in acc.machines:
        row = rows.get(n)
        pending = bool(row and row["status"] == "pending")
        usage = usage_today.get(n, {})
        prompt = None
        if (include_signin and pending and row["signin_link"] and row["signin_expires_at"]
                and row["signin_expires_at"] > now):
            prompt = {"link": row["signin_link"], "code": row["signin_code"],
                      "expires_at": row["signin_expires_at"].isoformat()}
        out.append({"node": n, "node_status": node_status.get(n, "offline"), "signed_in": not pending,
                    "sign_in_pending": pending, "shared_assignment": bool(row),
                    "removable": not machine_managed and (n == acc.node or bool(row)),
                    "runs_active": running.get(n, 0), "runs_24h": recent.get(n, 0),
                    "tokens_today": int(usage.get("tokens", 0)), "cost_today": float(usage.get("cost", 0)),
                    "state": ((row or {}).get("worker_state") or {}).get("state", ""), "signin": prompt,
                    "credential_expires_at": (row["credential_expires_at"].isoformat()
                                              if row and row["credential_expires_at"] else None)})
    return out


def machines_views(db, accounts: list[Account], node_status: dict[str, str], *,
                   include_signin: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Read machine details for a fleet snapshot in batches instead of five queries per account."""
    ids = list(dict.fromkeys(acc.id for acc in accounts if acc))
    if not ids:
        return {}
    machines_by_account: dict[str, dict[str, dict]] = {}
    for row in db.all("SELECT * FROM account_machines WHERE account_id=ANY(%s)", (ids,)):
        machines_by_account.setdefault(str(row["account_id"]), {})[str(row["node"])] = row
    active: dict[tuple[str, str], int] = {}
    for row in db.all("SELECT account_id,node,count(*) AS n FROM runs WHERE account_id=ANY(%s)"
                      " AND status='running' GROUP BY account_id,node", (ids,)):
        active[(str(row["account_id"]), str(row["node"]))] = int(row["n"])
    managed = {str(row["id"]) for row in db.all(
        "SELECT id FROM managed_ai_accounts WHERE id=ANY(%s) AND removed_at IS NULL", (ids,))}
    recent: dict[tuple[str, str], int] = {}
    for row in db.all("SELECT account_id,node,count(*) AS n FROM runs WHERE account_id=ANY(%s)"
                      " AND started_at > now() - interval '24 hours' GROUP BY account_id,node", (ids,)):
        recent[(str(row["account_id"]), str(row["node"]))] = int(row["n"])
    usage: dict[tuple[str, str], dict] = {}
    for row in db.all("SELECT account_id,node,coalesce(sum(tokens_in + tokens_out),0) AS tokens,"
                      "coalesce(sum(cost_usd),0) AS cost FROM runs WHERE account_id=ANY(%s)"
                      " AND started_at >= date_trunc('day',now()) GROUP BY account_id,node", (ids,)):
        usage[(str(row["account_id"]), str(row["node"]))] = row

    now = datetime.now(timezone.utc)
    result: dict[str, list[dict[str, Any]]] = {}
    for acc in accounts:
        rows = machines_by_account.get(acc.id, {})
        out = []
        for node in acc.machines:
            row = rows.get(node)
            pending = bool(row and row["status"] == "pending")
            node_usage = usage.get((acc.id, node), {})
            prompt = None
            if (include_signin and pending and row["signin_link"] and row["signin_expires_at"]
                    and row["signin_expires_at"] > now):
                prompt = {"link": row["signin_link"], "code": row["signin_code"],
                          "expires_at": row["signin_expires_at"].isoformat()}
            out.append({"node": node, "node_status": node_status.get(node, "offline"), "signed_in": not pending,
                        "sign_in_pending": pending, "shared_assignment": bool(row),
                        "removable": acc.id not in managed and (node == acc.node or bool(row)),
                        "runs_active": active.get((acc.id, node), 0), "runs_24h": recent.get((acc.id, node), 0),
                        "tokens_today": int(node_usage.get("tokens", 0)),
                        "cost_today": float(node_usage.get("cost", 0)),
                        "state": ((row or {}).get("worker_state") or {}).get("state", ""), "signin": prompt,
                        "credential_expires_at": (row["credential_expires_at"].isoformat()
                                                  if row and row["credential_expires_at"] else None)})
        result[acc.id] = out
    return result


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


class SigninCompleteBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)


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
        # same lock as claim_run: a claim sees either the old or the new limit, never a half-applied change
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + acc.id,))
                active = int(conn.execute("SELECT count(*) AS n FROM runs WHERE account_id=%s AND status='running'",
                                          (acc.id,)).fetchone()["n"])
                if body.session_limit < active:
                    raise HTTPException(409, {"error": "limit_below_active_sessions",
                                              "message": f"{acc.id} has {active} running sessions; wait for "
                                                         f"{active - body.session_limit} to finish before lowering "
                                                         f"the limit to {body.session_limit}."})
                conn.execute("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit,"
                             " managed, created_by) VALUES (%s,%s,%s,%s,%s,%s,false,%s)"
                             " ON CONFLICT (id) DO UPDATE SET session_limit=EXCLUDED.session_limit",
                             (acc.id, acc.provider, acc.tier, acc.roles, acc.explicit_only, body.session_limit,
                              user.subject))
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
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + acc.id,))
                conn.execute("DELETE FROM account_machine_exclusions WHERE account_id=%s AND node=%s",
                             (acc.id, body.node))
                conn.execute("INSERT INTO account_machines (account_id, node, enabled_by) VALUES (%s,%s,%s)",
                             (acc.id, body.node, user.subject))
        sync()
        audit("account", "enable_machine", acc.id, "", None, user.subject, True, body.node)
        ctx.events.emit("account.machine_enabled", account=acc.id, node=body.node, outcome="pending")
        return view(account_or_404(account_id), include_signin=True)

    @app.post("/v1/shared-accounts/{account_id}/machines/{node}/signin")
    def retry_signin(account_id: str, node: str, request: Request) -> dict:
        """Start a fresh sign-in attempt on one machine: a new request id makes its agent run the provider login
        again (a failed or expired attempt otherwise leaves the machine waiting without a link)."""
        user = console_user(request, {"nodes.manage"}, target="account", action="retry_signin", node=account_id)
        acc = account_or_404(account_id)
        changed = ctx.db.run(
            "UPDATE account_machines SET signin_attempt=signin_attempt + 1, signin_link='', signin_code='',"
            " signin_expires_at=NULL WHERE account_id=%s AND node=%s AND status='pending'", (acc.id, node))
        if not changed:
            raise HTTPException(409, f"{acc.id} has no pending sign-in on {node}")
        audit("account", "retry_signin", acc.id, "", None, user.subject, True, node)
        return view(account_or_404(account_id), include_signin=True)

    @app.delete("/v1/shared-accounts/{account_id}/machines/{node}")
    def remove_machine(account_id: str, node: str, request: Request, confirm: bool = False) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="remove_machine", node=account_id)
        acc = account_or_404(account_id)
        if not acc.runs_on(node):
            raise HTTPException(404, f"{acc.id} is not enabled on {node}")
        if ctx.db.one("SELECT 1 AS x FROM managed_ai_accounts WHERE id=%s AND removed_at IS NULL", (acc.id,)):
            raise HTTPException(409, f"{acc.id} is managed from the machine account screen")
        last = len(acc.machines) == 1
        if last and not confirm:
            raise HTTPException(409, {"error": "confirmation_required",
                                      "message": f"{node} is the last machine of {acc.id}; removing it removes the "
                                                 "account from every machine. Confirm to continue."})
        detach_machine(acc, node, user.subject)
        removed_account = False
        if last:
            removed_account = delete_account(acc)
        audit("account", "remove_machine", acc.id, "", None, user.subject, True, node,
              {"account_removed": removed_account})
        ctx.events.emit("account.machine_removed", account=acc.id, node=node, outcome="removed")
        return {"account": acc.id, "node": node, "account_removed": removed_account,
                "machines": [] if removed_account else [n for n in acc.machines if n != node]}

    def detach_machine(acc: Account, node: str, actor: str) -> None:
        """Remove one assignment and persist the opt-out for a configured primary machine."""
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + acc.id,))
                conn.execute("DELETE FROM account_machines WHERE account_id=%s AND node=%s", (acc.id, node))
                conn.execute("INSERT INTO account_machine_exclusions (account_id,node,detached_by) VALUES (%s,%s,%s)"
                             " ON CONFLICT (account_id,node) DO UPDATE SET detached_by=EXCLUDED.detached_by,"
                             " detached_at=now()", (acc.id, node, actor))

    def delete_account(acc: Account) -> bool:
        # Match claim_run's lock: once deletion returns, no stale account snapshot can insert a new run.
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + acc.id,))
                row = conn.execute("SELECT managed FROM shared_accounts WHERE id=%s FOR UPDATE",
                                   (acc.id,)).fetchone()
                if not row or not row["managed"]:
                    return False
                conn.execute("DELETE FROM account_machines WHERE account_id=%s", (acc.id,))
                conn.execute("DELETE FROM account_machine_exclusions WHERE account_id=%s", (acc.id,))
                conn.execute("DELETE FROM shared_accounts WHERE id=%s", (acc.id,))
                conn.execute("DELETE FROM accounts WHERE id=%s", (acc.id,))
        return True

    @app.delete("/v1/shared-accounts/{account_id}")
    def remove_account(account_id: str, request: Request, confirm: bool = False) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="remove", node=account_id)
        acc = account_or_404(account_id)
        if ctx.db.one("SELECT 1 AS x FROM managed_ai_accounts WHERE id=%s AND removed_at IS NULL", (acc.id,)):
            raise HTTPException(409, f"{acc.id} is managed from the machine account screen")
        row = ctx.db.one("SELECT managed FROM shared_accounts WHERE id=%s", (acc.id,))
        if not confirm:
            raise HTTPException(409, {"error": "confirmation_required",
                                      "message": f"Removing {acc.id} removes it from all {len(acc.machines)} "
                                                 "machines. Confirm to continue."})
        if row and row["managed"]:
            delete_account(acc)
            retained = False
        else:
            for node in acc.machines:
                detach_machine(acc, node, user.subject)
            retained = True
        audit("account", "remove", acc.id, "", None, user.subject, True, "removed from all machines",
              {"source_retained": retained})
        ctx.events.emit("account.removed", account=acc.id, outcome="removed")
        return {"account": acc.id, "removed": True, "source_retained": retained, "machines": list(acc.machines)}

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

    @app.post("/v1/accounts/{account_id}/signin-complete")
    def signin_complete(account_id: str, body: SigninCompleteBody, node: str = Depends(node_auth)) -> dict:
        """A successful provider login unlocks the local worker's credential probe for this machine."""
        acc = ctx.repo().account(account_id)
        if not acc or not acc.runs_on(node):
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        row = ctx.db.one("SELECT status,signin_attempt FROM account_machines WHERE account_id=%s AND node=%s",
                         (acc.id, node))
        if not row:
            raise HTTPException(409, f"{acc.id} has no shared sign-in on {node}")
        expected = f"shared:{node}:{acc.id}:{row['signin_attempt']}"
        if body.request_id != expected:
            raise HTTPException(409, "shared sign-in attempt is no longer current")
        if row["status"] == "pending":
            changed = ctx.db.run(
                "UPDATE account_machines SET status='signed_in', signed_in_at=now(), signin_link='', signin_code='',"
                " signin_expires_at=NULL WHERE account_id=%s AND node=%s AND status='pending' AND signin_attempt=%s",
                (acc.id, node, row["signin_attempt"]))
            if not changed:
                current = ctx.db.one("SELECT status,signin_attempt FROM account_machines"
                                     " WHERE account_id=%s AND node=%s", (acc.id, node))
                if not current or current["signin_attempt"] != row["signin_attempt"] or current["status"] != "signed_in":
                    raise HTTPException(409, "shared sign-in attempt is no longer current")
            else:
                ctx.events.emit("account.machine_signed_in", account=acc.id, node=node, outcome="completed")
        elif row["status"] != "signed_in":
            raise HTTPException(409, f"{acc.id} is not waiting for sign-in on {node}")
        # Provider-owned login establishes auth health on this machine, without creating quota data.
        ctx.db.run("UPDATE accounts SET last_probe_at=now(),status=CASE WHEN status='signed_out' THEN 'idle'"
                   " ELSE status END,last_error=CASE WHEN status='signed_out' THEN '' ELSE last_error END,"
                   "updated_at=now() WHERE id=%s", (acc.id,))
        return {"completed": True, "account": acc.id, "node": node}
