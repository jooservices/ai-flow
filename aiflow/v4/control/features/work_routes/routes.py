"""Routes of the work_routes feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from datetime import timedelta
from types import SimpleNamespace

from .bodies import ClaimBody
from typing import Any
from fastapi import Depends, HTTPException
from ... import (capacity_routing as capacity_routing_service, licensing, load_policy_api,
               memory, role_health, rollout, shared_accounts)
from ... import triage_claim as triage_claim_service
from ... import capacity, worker_candidates
from ...features.work.constants import BACKOFF_SQL, PRIORITY, utcnow
from ...features.work.claim import claim as claim_work
from ...features.work.eligible import eligible as eligible_tickets


def install(app, ctx, deps) -> SimpleNamespace:
    def _releases():
        from ...api import RELEASES  # noqa: PLC0415 - read when a route runs, so tests may patch it
        return RELEASES

    from ...api import MASTER_CYCLE_S, MASTER_SCOPE, log  # noqa: PLC0415 - api imports this module
    account_master_paused = deps.account_master_paused
    account_quota_at_risk = deps.account_quota_at_risk
    bot_identity = deps.bot_identity
    capacity_history = deps.capacity_history
    master_budget_state = deps.master_budget_state
    node_auth = deps.node_auth
    projects_routes = deps.projects_routes
    quota_forecast_at_risk = deps.quota_forecast_at_risk
    quota_peer_accounts = deps.quota_peer_accounts
    # --- work -----------------------------------------------------------------------------------------------
    def last_dev(key: str, done: bool = False) -> dict | None:
        """The latest finished dev run; done=True: the latest one that delivered work (a failed or blocked run
        in between does not answer the reviewer's findings)."""
        return ctx.db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
                          + (" AND outcome='done'" if done else "") + " ORDER BY started_at DESC LIMIT 1", (key,))

    def eligible(account_id: str, node: str, **options) -> list[dict]:
        """Tickets this account may take now (rules in features/work/eligible.py)."""
        return eligible_tickets(ctx, work_deps, account_id, node, **options)

    def route_capacity(account_id: str, node: str, offered: list[dict],
                       ticket_key: str | None = None) -> list[dict]:
        """Prefer an eligible account whose quota can cover the ticket's estimated upper bound.

        Candidate polling keeps its legacy behavior when no reliable quota forecast exists. If at least one eligible
        peer has a quota-safe forecast, only the best safe account sees that development ticket.
        """
        development = [ticket for ticket in offered if ticket.get("step") == "dev"]
        if not development:
            return offered
        # A partial index keeps the common no-forecast case cheap: candidate polling only reads
        # accounts whose saved forecast can actually influence routing.
        account_rows = ctx.db.all(
            "SELECT id,provider,tier,roles,quota_forecast FROM accounts WHERE quota_forecast->>'source'='reported'"
            " OR quota_forecast->>'confidence' IN ('medium','high') ORDER BY id")
        accounts_by_id = {str(row["id"]): row for row in account_rows}
        forecasts = {str(row["id"]): row.get("quota_forecast") or {} for row in account_rows}
        # Without reliable forecasts, quota routing cannot change the legacy offer. Avoid reading every account,
        # walking peer eligibility and loading historical capacity on each worker poll.
        if not account_rows:
            return offered
        if account_id not in accounts_by_id:
            current = ctx.db.one("SELECT id,provider,tier,roles,quota_forecast FROM accounts WHERE id=%s",
                                 (account_id,))
            if current:
                accounts_by_id[account_id] = current
                forecasts[account_id] = current.get("quota_forecast") or {}
        forecasts = capacity.with_active_quota_reservations(ctx.db, forecasts)
        samples = capacity_history()
        eligible_by_key: dict[tuple[str, str], dict[str, dict]] = {}
        for account in ctx.repo().accounts:
            if account.id not in accounts_by_id:
                continue
            machines = [node] if account.id == account_id else account.machines
            for machine in dict.fromkeys(machines):
                if account.id != account_id and (machine not in shared_accounts.online_nodes(ctx.db)
                                                 or not rollout.node_compatible(ctx, machine, _releases())["compatible"]):
                    continue
                try:
                    peer_offers = offered if account.id == account_id and machine == node else eligible(
                        account.id, machine, routing=False, capacity_routing=False, ticket_key=ticket_key)
                except HTTPException:
                    continue
                for item in peer_offers:
                    if item.get("step") == "dev":
                        eligible_by_key.setdefault((item["key"], item["step"]), {})[account.id] = {
                            **accounts_by_id[account.id], "tier": account.tier}
        if not eligible_by_key:
            return offered
        selected = capacity_routing_service.select_accounts(development, eligible_by_key, samples, forecasts,
                                                            ctx.provider_eligibility.state(persist=False))
        return [ticket for ticket in offered
                if ticket.get("step") != "dev" or (ticket["key"], ticket["step"]) not in selected
                or account_id in selected[(ticket["key"], ticket["step"])]]

    # What the eligibility rules of features/work/eligible.py need from this app; read when a rule runs, so tests may patch _releases().
    work_deps = SimpleNamespace(
        eligible=eligible, account_master_paused=account_master_paused, last_dev=last_dev,
        quota_forecast_at_risk=quota_forecast_at_risk, quota_peer_accounts=quota_peer_accounts,
        route_capacity=route_capacity, ticket_epic=lambda t: projects_routes.ticket_epic(t), releases=lambda: _releases())

    def master_ready(account_id: str, node: str) -> bool:
        if ctx.leases.cooling(account_id) or account_master_paused(account_id):
            return False
        ready = ctx.db.one("SELECT a.status, n.status AS node_status FROM accounts a, nodes n"
                           " WHERE a.id=%s AND n.name=%s", (account_id, node))
        return bool(ready) and ready["status"] in ("idle", "working", "probing") and ready["node_status"] == "online"

    def master_candidate(account_id: str, node: str) -> dict | None:
        repo = ctx.repo()
        account = repo.account(account_id)
        if not account or not account.runs_on(node):
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if account.paused or "master" not in (account.roles or []) or not master_ready(account_id, node):
            return None
        if account_quota_at_risk(account_id, {"master"}) or (account_id, "master") in role_health.disabled_roles(ctx.db):
            return None
        if not shared_accounts.may_take(ctx.db, account, node)[0]:
            return None
        if not master_budget_state()["enabled"]:
            return None
        held = set(ctx.leases.held_keys())
        if MASTER_SCOPE in held:
            return None
        recent = ctx.db.one("SELECT id, fence, ticket_key, provider, status, outcome, ended_at FROM runs"
                            " WHERE step='master' ORDER BY started_at DESC LIMIT 1")
        failover = False
        if recent:
            lease_lost = (recent["status"] == "running" and
                          ctx.leases.master_holder() != f"{recent['id']}:{recent['fence']}")
            failed_over = recent["outcome"] in ("quota", "lost") or lease_lost
            # Prefer the other provider for one cycle after a failure, and only while it has a ready master
            # account. Otherwise any ready master account may take over: cooldown and node checks above
            # already exclude the account that failed, so the flow never waits on an unavailable provider.
            failed_at = recent["ended_at"] or utcnow()
            if (failed_over and recent["provider"] == account.provider
                    and failed_at > utcnow() - timedelta(seconds=MASTER_CYCLE_S)
                    and any(a.provider != account.provider and "master" in (a.roles or [])
                            and any(master_ready(a.id, m) for m in a.machines) for a in repo.accounts)):
                return None
            # A lost or quota-stopped holder is an account failure, not a failing cycle: no scope backoff.
            failover = failed_over
        backoff = ctx.db.all(BACKOFF_SQL, {"step": "master"})
        if not failover and any(row["ticket_key"] == MASTER_SCOPE for row in backoff):
            return None
        if recent and recent["ended_at"] and recent["ended_at"] > utcnow() - timedelta(seconds=MASTER_CYCLE_S):
            if recent["outcome"] == "done":
                return None
        return {"key": MASTER_SCOPE, "project": "", "status": "Master", "type": "Master", "priority": "Highest",
                "labels": [], "summary": "Master monitoring cycle",
                "description": "Review incidents and follow up on blocked or slow work.",
                "jira_created": None, "step": "master"}

    def entitled_candidates(account: str, node: str, worker_id: str, state: dict) -> tuple[list[dict], str]:
        """Filter offers by account, machine, and project plan limits without affecting active runs."""
        candidates = eligible(account, node)
        project_priorities = {key: int(getattr(project, "priority", 5) or 5)
                              for key, project in ctx.repo().projects.items()}
        allowed: list[dict] = []
        refused: list[str] = []
        for ticket in candidates:
            try:
                licensing.enforce_project_entitlement(ctx, ticket["project"],
                                                      public_key=ctx.settings.license_public_key, state=state)
            except licensing.LicenseError as exc:
                refused.append(str(exc))
            else:
                if triage_claim_service.filter_candidates(ctx, [ticket]):  # a dev ticket waits for a verified owner answer
                    allowed.append(ticket)
        allowed = worker_candidates.diversify_dev_candidates(allowed, account, worker_id,
                                                              project_priorities, PRIORITY)
        reason = refused[0] if candidates and not allowed and refused else ""
        return allowed, reason

    load_policy_api.register_worker_candidate_routes(app, ctx, node_auth, _releases(), entitled_candidates,
                                                     master_candidate)


    def related_hits(t: dict) -> list[dict]:
        """Up to 3 past entries related to the ticket summary; memory is advice, so an error gives none."""
        try:
            return memory.related(ctx.db, t["project"], t["summary"], exclude_ticket=t["key"], limit=3)
        except Exception as e:  # noqa: BLE001
            log.warning("memory lookup for %s failed: %s", t["key"], e)
            return []

    @app.post("/v1/work/claim")
    def claim(body: ClaimBody, node: str = Depends(node_auth)) -> Any:
        work_deps.master_candidate, work_deps.capacity_history = master_candidate, capacity_history
        work_deps.related_hits, work_deps.bot_identity = related_hits, bot_identity
        return claim_work(ctx, work_deps, body, node)

    return SimpleNamespace(last_dev=last_dev, eligible=eligible, route_capacity=route_capacity, work_deps=work_deps, master_ready=master_ready, master_candidate=master_candidate, entitled_candidates=entitled_candidates, related_hits=related_hits, claim=claim)
