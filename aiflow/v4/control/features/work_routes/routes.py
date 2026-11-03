"""Work candidate and claim routes, split into explicit route-group installers."""

from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from ... import (
    capacity_routing as capacity_routing_service,
    licensing,
    load_policy_api,
    memory,
    role_health,
    rollout,
    shared_accounts,
)
from ... import triage_claim as triage_claim_service
from ... import capacity, worker_candidates
from ...features.work.claim import claim as claim_work
from ...features.work.constants import BACKOFF_SQL, PRIORITY, utcnow
from ...features.work.eligible import eligible as eligible_tickets
from .routes_candidates import install_candidate_routes
from .routes_claim import install_claim_routes


@dataclass(frozen=True)
class WorkRouteRuntime:
    """The app-scoped values shared by route helpers and their installers."""

    ctx: Any
    account_master_paused: Any
    account_quota_at_risk: Any
    capacity_history: Any
    master_budget_state: Any
    projects_routes: Any
    quota_forecast_at_risk: Any
    quota_peer_accounts: Any
    master_scope: str
    master_cycle_s: int
    logger: Any


def _releases():
    from ...api import RELEASES  # noqa: PLC0415 - read when a route runs, so tests may patch it

    return RELEASES


def _route_releases():
    return _releases()


def _last_dev(ctx, key: str, done: bool = False) -> dict | None:
    """The latest finished dev run; done=True: the latest one that delivered work (a failed or blocked run
    in between does not answer the reviewer's findings)."""
    return ctx.db.one(
        "SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
        + (" AND outcome='done'" if done else "")
        + " ORDER BY started_at DESC LIMIT 1",
        (key,),
    )


def _eligible(runtime: WorkRouteRuntime, work_deps, account_id: str, node: str, **options) -> list[dict]:
    """Tickets this account may take now (rules in features/work/eligible.py)."""
    return eligible_tickets(runtime.ctx, work_deps, account_id, node, **options)


def _eligible_by_account(
    runtime: WorkRouteRuntime,
    work_deps,
    offered: list[dict],
    accounts_by_id: dict,
    account_id: str,
    node: str,
    ticket_key: str | None,
) -> dict[tuple[str, str], dict[str, dict]]:
    eligible_by_key: dict[tuple[str, str], dict[str, dict]] = {}
    for account in runtime.ctx.repo().accounts:
        if account.id not in accounts_by_id:
            continue
        machines = [node] if account.id == account_id else account.machines
        for machine in dict.fromkeys(machines):
            if account.id != account_id and (
                machine not in shared_accounts.online_nodes(runtime.ctx.db)
                or not rollout.node_compatible(runtime.ctx, machine, _releases())["compatible"]
            ):
                continue
            try:
                peer_offers = (
                    offered
                    if account.id == account_id and machine == node
                    else work_deps.eligible(
                        account.id, machine, routing=False, capacity_routing=False, ticket_key=ticket_key
                    )
                )
            except HTTPException:
                continue
            for item in peer_offers:
                if item.get("step") == "dev":
                    eligible_by_key.setdefault((item["key"], item["step"]), {})[account.id] = {
                        **accounts_by_id[account.id],
                        "tier": account.tier,
                    }
    return eligible_by_key


def _route_capacity(
    runtime: WorkRouteRuntime, work_deps, account_id: str, node: str, offered: list[dict], ticket_key: str | None = None
) -> list[dict]:
    """Prefer an eligible account whose quota can cover the ticket's estimated upper bound.

    Candidate polling keeps its legacy behavior when no reliable quota forecast exists. If at least one eligible
    peer has a quota-safe forecast, only the best safe account sees that development ticket.
    """
    ctx = runtime.ctx
    development = [ticket for ticket in offered if ticket.get("step") == "dev"]
    if not development:
        return offered
    # A partial index keeps the common no-forecast case cheap: candidate polling only reads accounts whose saved
    # forecast can actually influence routing.
    account_rows = ctx.db.all(
        "SELECT id,provider,tier,roles,quota_forecast FROM accounts WHERE quota_forecast->>'source'='reported'"
        " OR quota_forecast->>'confidence' IN ('medium','high') ORDER BY id"
    )
    accounts_by_id = {str(row["id"]): row for row in account_rows}
    forecasts = {str(row["id"]): row.get("quota_forecast") or {} for row in account_rows}
    # Without reliable forecasts, quota routing cannot change the legacy offer. Avoid reading every account,
    # walking peer eligibility and loading historical capacity on each worker poll.
    if not account_rows:
        return offered
    if account_id not in accounts_by_id:
        current = ctx.db.one("SELECT id,provider,tier,roles,quota_forecast FROM accounts WHERE id=%s", (account_id,))
        if current:
            accounts_by_id[account_id] = current
            forecasts[account_id] = current.get("quota_forecast") or {}
    forecasts = capacity.with_active_quota_reservations(ctx.db, forecasts)
    samples = runtime.capacity_history()
    eligible_by_key = _eligible_by_account(runtime, work_deps, offered, accounts_by_id, account_id, node, ticket_key)
    if not eligible_by_key:
        return offered
    selected = capacity_routing_service.select_accounts(
        development, eligible_by_key, samples, forecasts, ctx.provider_eligibility.state(persist=False)
    )
    return [
        ticket
        for ticket in offered
        if ticket.get("step") != "dev"
        or (ticket["key"], ticket["step"]) not in selected
        or account_id in selected[(ticket["key"], ticket["step"])]
    ]


def _master_ready(runtime: WorkRouteRuntime, account_id: str, node: str) -> bool:
    ctx = runtime.ctx
    if ctx.leases.cooling(account_id) or runtime.account_master_paused(account_id):
        return False
    ready = ctx.db.one(
        "SELECT a.status, n.status AS node_status FROM accounts a, nodes n WHERE a.id=%s AND n.name=%s",
        (account_id, node),
    )
    return bool(ready) and ready["status"] in ("idle", "working", "probing") and ready["node_status"] == "online"


def _master_candidate(runtime: WorkRouteRuntime, account_id: str, node: str) -> dict | None:
    ctx = runtime.ctx
    repo = ctx.repo()
    account = repo.account(account_id)
    if not account or not account.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    if account.paused or "master" not in (account.roles or []) or not _master_ready(runtime, account_id, node):
        return None
    if runtime.account_quota_at_risk(account_id, {"master"}) or (account_id, "master") in role_health.disabled_roles(
        ctx.db
    ):
        return None
    if not shared_accounts.may_take(ctx.db, account, node)[0]:
        return None
    if not runtime.master_budget_state()["enabled"]:
        return None
    held = set(ctx.leases.held_keys())
    if runtime.master_scope in held:
        return None
    recent = ctx.db.one(
        "SELECT id, fence, ticket_key, provider, status, outcome, ended_at FROM runs"
        " WHERE step='master' ORDER BY started_at DESC LIMIT 1"
    )
    failover = False
    if recent:
        lease_lost = recent["status"] == "running" and ctx.leases.master_holder() != f"{recent['id']}:{recent['fence']}"
        failed_over = recent["outcome"] in ("quota", "lost") or lease_lost
        # Prefer the other provider for one cycle after a failure, and only while it has a ready master
        # account. Otherwise any ready master account may take over: cooldown and node checks above
        # already exclude the account that failed, so the flow never waits on an unavailable provider.
        failed_at = recent["ended_at"] or utcnow()
        if (
            failed_over
            and recent["provider"] == account.provider
            and failed_at > utcnow() - timedelta(seconds=runtime.master_cycle_s)
            and any(
                a.provider != account.provider
                and "master" in (a.roles or [])
                and any(_master_ready(runtime, a.id, m) for m in a.machines)
                for a in repo.accounts
            )
        ):
            return None
        # A lost or quota-stopped holder is an account failure, not a failing cycle: no scope backoff.
        failover = failed_over
    backoff = ctx.db.all(BACKOFF_SQL, {"step": "master"})
    if not failover and any(row["ticket_key"] == runtime.master_scope for row in backoff):
        return None
    if (
        recent
        and recent["ended_at"]
        and recent["ended_at"] > utcnow() - timedelta(seconds=runtime.master_cycle_s)
        and recent["outcome"] == "done"
    ):
        return None
    return {
        "key": runtime.master_scope,
        "project": "",
        "status": "Master",
        "type": "Master",
        "priority": "Highest",
        "labels": [],
        "summary": "Master monitoring cycle",
        "description": "Review incidents and follow up on blocked or slow work.",
        "jira_created": None,
        "step": "master",
    }


def _entitled_candidates(
    runtime: WorkRouteRuntime, work_deps, account: str, node: str, worker_id: str, state: dict
) -> tuple[list[dict], str]:
    """Filter offers by account, machine, and project plan limits without affecting active runs."""
    ctx = runtime.ctx
    candidates = work_deps.eligible(account, node)
    project_priorities = {
        key: int(getattr(project, "priority", 5) or 5) for key, project in ctx.repo().projects.items()
    }
    allowed: list[dict] = []
    refused: list[str] = []
    for ticket in candidates:
        try:
            licensing.enforce_project_entitlement(
                ctx, ticket["project"], public_key=ctx.settings.license_public_key, state=state
            )
        except licensing.LicenseError as exc:
            refused.append(str(exc))
        else:
            if triage_claim_service.filter_candidates(ctx, [ticket]):  # a dev ticket waits for a verified owner answer
                allowed.append(ticket)
    allowed = worker_candidates.diversify_dev_candidates(allowed, account, worker_id, project_priorities, PRIORITY)
    reason = refused[0] if candidates and not allowed and refused else ""
    return allowed, reason


def _related_hits(runtime: WorkRouteRuntime, ticket: dict) -> list[dict]:
    """Up to 3 past entries related to the ticket summary; memory is advice, so an error gives none."""
    try:
        return memory.related(
            runtime.ctx.db, ticket["project"], ticket["summary"], exclude_ticket=ticket["key"], limit=3
        )
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("memory lookup for %s failed: %s", ticket["key"], exc)
        return []


def _ticket_epic(projects_routes, ticket):
    return projects_routes.ticket_epic(ticket)


def _call_claim_work(ctx, work_deps, body, node):
    return claim_work(ctx, work_deps, body, node)


def _build_helpers(runtime: WorkRouteRuntime) -> SimpleNamespace:
    work_deps = SimpleNamespace()
    last_dev = partial(_last_dev, runtime.ctx)
    eligible = partial(_eligible, runtime, work_deps)
    route_capacity = partial(_route_capacity, runtime, work_deps)
    master_ready = partial(_master_ready, runtime)
    master_candidate = partial(_master_candidate, runtime)
    entitled_candidates = partial(_entitled_candidates, runtime, work_deps)
    related_hits = partial(_related_hits, runtime)
    work_deps.eligible = eligible
    work_deps.account_master_paused = runtime.account_master_paused
    work_deps.last_dev = last_dev
    work_deps.quota_forecast_at_risk = runtime.quota_forecast_at_risk
    work_deps.quota_peer_accounts = runtime.quota_peer_accounts
    work_deps.route_capacity = route_capacity
    work_deps.ticket_epic = partial(_ticket_epic, runtime.projects_routes)
    work_deps.releases = _route_releases
    return SimpleNamespace(
        last_dev=last_dev,
        eligible=eligible,
        route_capacity=route_capacity,
        work_deps=work_deps,
        master_ready=master_ready,
        master_candidate=master_candidate,
        entitled_candidates=entitled_candidates,
        related_hits=related_hits,
    )


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import MASTER_CYCLE_S, MASTER_SCOPE, log  # noqa: PLC0415 - api imports this module

    runtime = WorkRouteRuntime(
        ctx,
        deps.account_master_paused,
        deps.account_quota_at_risk,
        deps.capacity_history,
        deps.master_budget_state,
        deps.projects_routes,
        deps.quota_forecast_at_risk,
        deps.quota_peer_accounts,
        MASTER_SCOPE,
        MASTER_CYCLE_S,
        log,
    )
    helpers = _build_helpers(runtime)
    install_candidate_routes(
        app,
        ctx,
        deps,
        load_policy_api.register_worker_candidate_routes,
        _releases(),
        helpers.entitled_candidates,
        helpers.master_candidate,
    )
    claim = install_claim_routes(
        app,
        ctx,
        deps,
        helpers.work_deps,
        helpers.master_candidate,
        runtime.capacity_history,
        helpers.related_hits,
        deps.bot_identity,
    )
    return SimpleNamespace(**vars(helpers), claim=claim)
