"""Named policy, acquisition and registration steps for the atomic work claim."""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ... import (capacity, dev_history, hotfiles, licensing, load_policy_api, role_health, rollout,
                 triage_claim as triage_claim_service)
from ...platform.events.wiring import publish_for
from .events import RunStarted
from ..work.constants import utcnow
from ...route_deps import h


@dataclass
class Claim:
    """Values shared by the ordered steps of one claim request."""

    ctx: Any
    deps: Any
    body: Any
    node: str
    store: Any
    load_capacity: Any = None
    state: Any = None
    ticket: dict[str, Any] | None = None
    repo: Any = None
    account: Any = None
    triage_context: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    owner_answer: dict[str, Any] | None = None
    probation_id: str | None = None
    lease: Any = None
    model: str | None = None
    effort: str | None = None
    run_token: str | None = None
    project: Any = None
    epic: str | None = None
    ticket_data: dict[str, Any] | None = None
    prior_dev: dict[str, Any] | None = None
    selected_repo: dict[str, Any] = field(default_factory=dict)
    run_details: dict[str, Any] = field(default_factory=dict)
    quota_reservation_tokens: int | None = None


def refuse_early(work: Claim) -> JSONResponse | None:
    """Apply lease, load, licence and version checks in their established order."""
    ctx, deps, body, node = work.ctx, work.deps, work.body, work.node
    if body.step != "master" and ctx.leases.holder(body.ticket):
        account = ctx.repo().account(body.account)
        if account and not account.runs_on(node):
            raise HTTPException(403, f"account {body.account} does not run on {node}")
        if account:
            return JSONResponse({"claimed": False, "reason": "ticket is already claimed"}, status_code=409)
    work.load_capacity, blocked = load_policy_api.work_claim_capacity(ctx, node)
    if blocked:
        return blocked
    try:
        work.state = licensing.enforce_work_entitlement(
            ctx, body.account, node, public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        return JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
    policy = rollout.node_compatible(ctx, node, deps.releases())
    if not policy["compatible"]:
        return JSONResponse({"claimed": False, "reason": f"version skew: {policy['reason']}"}, status_code=409)
    return None


def select_ticket(work: Claim) -> JSONResponse | None:
    """Select and authorize the requested ticket before creating its run identity."""
    ctx, deps, body, node = work.ctx, work.deps, work.body, work.node
    if body.step == "master":
        candidates = [deps.master_candidate(body.account, node)]
    else:
        candidates = deps.eligible(body.account, node, ticket_key=body.ticket, lease_checked=True,
                                  step_filter=body.step)
        offered = next((candidate for candidate in candidates
                        if candidate["key"] == body.ticket and candidate["step"] == body.step), None)
        if offered:
            try:
                licensing.enforce_project_entitlement(
                    ctx, offered["project"], public_key=ctx.settings.license_public_key, state=work.state)
            except licensing.LicenseError as exc:
                return JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
    tickets = {(ticket["key"], ticket["step"]): ticket for ticket in candidates if ticket}
    ticket = tickets.get((body.ticket, body.step))
    if not ticket or (body.step == "dev" and not triage_claim_service.development_eligible(ctx, ticket)):
        return JSONResponse({"claimed": False, "reason": "not eligible"}, status_code=409)
    if body.step == "dev":
        authorized, work.owner_answer = triage_claim_service.authorize_development(ctx, ticket)
        if not authorized:
            return JSONResponse({"claimed": False, "reason": "waiting for a verified owner answer"},
                                status_code=409)
    work.ticket = ticket
    work.repo = ctx.repo()
    work.account = work.repo.account(body.account)
    work.triage_context = (triage_claim_service.context(ctx, ticket) if body.step == "triage" else {})
    work.run_id = str(uuid.uuid4())
    return None


def acquire_probation(work: Claim) -> JSONResponse | None:
    """Assign the one-at-a-time probation ticket before acquiring its lease."""
    ticket = work.ticket
    if ticket.get("probation"):
        probation_id = role_health.assign_probation(
            work.ctx.db, work.account.id, role_health.role_of(work.body.step) or "", ticket["key"])
        if probation_id is None:
            return JSONResponse({"claimed": False, "reason": "a probation ticket is already running"},
                                status_code=409)
        work.probation_id = probation_id
    return None


def acquire_lease(work: Claim) -> JSONResponse | None:
    """Acquire the fenced lease and drop assigned probation if the lease was lost."""
    work.lease = work.ctx.leases.claim(work.ticket["key"], work.run_id, work.ctx.settings.lease_ms)
    if not work.lease.ok:
        if work.probation_id is not None:
            role_health.drop_probation(work.ctx.db, work.probation_id)
        return JSONResponse({"claimed": False, "reason": f"held by {work.lease.holder}"}, status_code=409)
    return None


def reserve_hot_files(work: Claim) -> JSONResponse | None:
    """Reserve development hot files and undo lease and probation when another run owns one."""
    if work.body.step == "dev":
        ticket = work.ticket
        taken = hotfiles.reserve(
            work.ctx.db, ticket["key"], work.run_id,
            hotfiles.predict(work.ctx.db, ticket, work.deps.ticket_epic(ticket))["hot"],
            slots=work.ctx.settings.hot_file_slots)
        if taken:
            work.ctx.leases.release(ticket["key"], work.run_id, work.lease.fence)
            if work.probation_id is not None:
                role_health.drop_probation(work.ctx.db, work.probation_id)
            return JSONResponse({"claimed": False, "reason": f"hot file {', '.join(taken[1])} is reserved by "
                                f"{taken[0]}"}, status_code=409)
    return None


def repository_settings(work: Claim) -> None:
    """Capture the ticket repository and prior development provenance for this run."""
    ticket, body = work.ticket, work.body
    work.model, work.effort = work.repo.model_for(
        work.account.provider, "senior" if "tier-senior" in ticket["labels"] else work.account.tier)
    work.run_token = secrets.token_urlsafe(32)
    work.project = work.repo.projects.get(ticket["project"])
    work.epic = work.deps.ticket_epic(ticket)
    work.ticket_data = {
        **{key: ticket[key] for key in ("key", "project", "summary", "description", "labels", "priority", "type")},
        "parent_key": ticket.get("parent_key"), "epic": work.epic}
    work.prior_dev = None if body.step == "master" else dev_history.delivered_or_last(
        work.ctx.db, ticket["key"])
    work.selected_repo = work.project.settings_for_ticket(work.ticket_data) if work.project else {}
    prior_repository = str(((work.prior_dev or {}).get("details") or {}).get("repository") or "")
    if work.project and prior_repository and body.step in {"dev", "verify", "qa", "triage", "post_merge"}:
        prior_settings = ((work.prior_dev or {}).get("details") or {}).get("repository_settings")
        work.selected_repo = (dict(prior_settings) if isinstance(prior_settings, dict)
                              else work.project.settings_for_repo(prior_repository))
    work.run_details = ({"repository": work.selected_repo.get("repo", ""),
                         "repository_settings": work.selected_repo} if work.selected_repo else {})


def dev_assignment(work: Claim) -> None:
    """Estimate a development assignment and preserve its established explanation text."""
    if work.body.step == "dev":
        samples = work.deps.capacity_history()
        estimate = capacity.estimate(work.ticket, samples)
        account_row = work.store.account_forecast(work.account.id)
        forecasts = {account_row.id: account_row.quota_forecast} if account_row else {}
        forecasts = capacity.with_active_quota_reservations(work.ctx.db, forecasts)
        fit = capacity.account_fit(work.account.id, forecasts.get(work.account.id, {}), estimate)
        expected_tokens, expected_minutes = estimate["tokens"], estimate["time_minutes"]
        work.quota_reservation_tokens = (int(expected_tokens["high"])
                                         if work.account.sessions > 1 and fit.get("left_tokens") is not None else None)
        if fit["known"] and fit["fits"]:
            senior_work = ("tier-senior" in (work.ticket.get("labels") or [])
                           or estimate["size"] in {"L", "XL"} or work.ticket.get("priority") == "Highest")
            tier_order = ("senior, then middle, then junior" if senior_work
                          else "middle, then senior, then junior")
            strength = f"tier preference for this ticket is {tier_order}"
            reason = (f"Assigned to {work.account.id} ({work.account.provider}, {work.account.tier}): estimated work is "
                      f"{expected_tokens['low']:,.0f}–{expected_tokens['high']:,.0f} tokens and "
                      f"{expected_minutes['low']:,.0f}–{expected_minutes['high']:,.0f} minutes; "
                      f"{fit['reason']}; {strength}.")
        elif fit["known"]:
            reason = (f"Assigned to {work.account.id} ({work.account.provider}, {work.account.tier}) because it remains eligible and no "
                      f"quota-safe eligible account was identified. The estimate is "
                      f"{expected_tokens['low']:,.0f}–{expected_tokens['high']:,.0f} tokens and "
                      f"{expected_minutes['low']:,.0f}–{expected_minutes['high']:,.0f} minutes; {fit['reason']}.")
        else:
            reason = (f"Assigned to {work.account.id} ({work.account.provider}, {work.account.tier}) by the existing eligibility rules. "
                      f"The estimate is {expected_tokens['low']:,.0f}–{expected_tokens['high']:,.0f} tokens and "
                      f"{expected_minutes['low']:,.0f}–{expected_minutes['high']:,.0f} minutes; "
                      "quota history is not reliable enough to compare eligible accounts.")
        work.run_details["capacity"] = {"estimate": estimate, "account_fit": fit,
                                        "assignment_reason": reason, "assigned_at": utcnow().isoformat()}
    else:
        work.quota_reservation_tokens = None


def register_run(work: Claim) -> JSONResponse | None:
    """Register the run, releasing its lease and hot files if capacity refuses it."""
    account, ticket, body = work.account, work.ticket, work.body
    values = (work.run_id, ticket["key"], ticket["project"], body.step, account.id, account.provider,
              work.model, work.node, work.lease.fence, h(work.run_token))
    accepted = load_policy_api.claim_run(
        work.ctx, account, values, work.load_capacity, run_details=work.run_details,
        quota_reservation_tokens=work.quota_reservation_tokens)
    if not accepted:
        work.ctx.leases.release(ticket["key"], work.run_id, work.lease.fence)
        hotfiles.unreserve(work.ctx.db, work.run_id)
        return JSONResponse({"claimed": False, "reason": f"account {account.id} is at its limit of {account.sessions}"
                            " concurrent sessions or machine load capacity is full"}, status_code=409)
    return None


def record_claim(work: Claim) -> None:
    """Record assignment, attach provenance and probation, mark the account working, and publish the run event."""
    ctx, store, ticket, body, account = work.ctx, work.store, work.ticket, work.body, work.account
    ctx.provider_eligibility.record_assignment(ticket, account, body.step)
    if work.run_details:
        store.set_run_details(work.run_id, work.run_details)
    if body.step == "dev" and work.prior_dev and work.prior_dev.get("pr_url") and work.prior_dev.get("branch"):
        store.inherit_pull_request(work.run_id, work.prior_dev)
    if work.probation_id is not None:
        role_health.attach_probation_run(ctx.db, work.probation_id, work.run_id)
        ctx.events.emit("role.probation", ticket=ticket["key"], project=ticket["project"], step=body.step,
                        run_id=work.run_id, account=account.id, outcome="assigned",
                        reason=f"probation ticket for role {role_health.role_of(body.step)}")
    store.mark_account_working(account.id)
    with ctx.db.transaction():
        publish_for(ctx, RunStarted(
            run_id=work.run_id, ticket_key=ticket["key"], project=ticket["project"], step=body.step,
            fence=work.lease.fence, account=account.id, provider=account.provider,
            model=work.model, node=work.node))
