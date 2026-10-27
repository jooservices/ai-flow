"""Claim a worker ticket after the API has authenticated its node and validated the request."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import (capacity, environment as environment_reports, escalations, hotfiles, knowledge, licensing, memory,
               role_health, rollout, shared_accounts, tooling, work_eligibility)
from ..core import handover
from .repositories.accounts import AccountsRepository
from .repositories.events import EventsRepository
from .repositories.nodes import NodesRepository
from .repositories.owner_decisions import OwnerDecisionsRepository
from .repositories.runs import RunsRepository
from .repositories.tickets import TicketsRepository

log = logging.getLogger("aiflow4.work_claim")


class ClaimRejected(Exception):
    """An authenticated worker cannot claim the requested ticket right now."""

    def __init__(self, reason: str, status_code: int = 409) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass
class ClaimState:
    body: Any
    node: str
    ticket: dict
    account: Any
    repo: Any
    project: Any
    ticket_data: dict
    epic: str
    prior_dev: dict | None
    selected_repo: dict
    run_details: dict
    triage_context: dict
    run_id: str
    probation_id: int | None
    fence: int
    run_token: str
    model: str
    effort: str


def claim(ctx, body, node: str, runtime, releases, *, master_candidate: Callable, last_dev: Callable,
          ticket_epic: Callable, bot_identity: Callable) -> dict:
    state, ticket = _eligible_ticket(ctx, body, node, runtime, releases, master_candidate)
    repo = ctx.repo()
    account = repo.account(body.account)
    triage_context = _triage_context(ctx, ticket, last_dev) if body.step == "triage" else {}
    run_id = str(uuid.uuid4())
    probation_id, lease = _claim_lease(ctx, ticket, account, body, run_id)
    _reserve_hotfiles(ctx, ticket, body, run_id, lease.fence, probation_id, ticket_epic)
    claim_state = _claim_state(ctx, body, node, ticket, account, repo, triage_context, run_id,
                               probation_id, lease.fence, last_dev, ticket_epic)
    if not _persist_claim(ctx, claim_state):
        _release_claim(ctx, ticket, run_id, lease.fence, probation_id)
        raise ClaimRejected(f"account {account.id} is at its limit of {account.sessions} concurrent sessions")
    extra = _step_context(ctx, claim_state)
    return _claim_response(ctx, claim_state, extra, bot_identity)


def _eligible_ticket(ctx, body, node, runtime, releases, master_candidate):
    try:
        license_state = licensing.enforce_work_entitlement(
            ctx, body.account, node, public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        raise ClaimRejected(str(exc)) from None
    policy = rollout.node_compatible(ctx, node, releases)
    if not policy["compatible"]:
        raise ClaimRejected(f"version skew: {policy['reason']}")
    candidates = _candidates(body, node, runtime, master_candidate)
    offered = next((candidate for candidate in candidates if candidate
                    and candidate["key"] == body.ticket and candidate["step"] == body.step), None)
    if offered:
        try:
            licensing.enforce_project_entitlement(
                ctx, offered["project"], public_key=ctx.settings.license_public_key, state=license_state)
        except licensing.LicenseError as exc:
            raise ClaimRejected(str(exc)) from None
    ticket = {(item["key"], item["step"]): item for item in candidates if item}.get((body.ticket, body.step))
    if not ticket:
        raise ClaimRejected("not eligible")
    return license_state, ticket


def _candidates(body, node, runtime, master_candidate):
    if body.step == "master":
        return [master_candidate(body.account, node)]
    try:
        return runtime.eligible(body.account, node)
    except work_eligibility.CandidateAccountUnavailable:
        raise


def _triage_context(ctx, ticket: dict, last_dev: Callable) -> dict:
    verify = RunsRepository(ctx.db).latest_verify_for_ticket(ticket["key"])
    context = {"last_dev_summary": (last_dev(ticket["key"]) or {}).get("summary", ""),
               "verify_findings": ((verify["details"] or {}).get("findings") or verify["summary"])
               if verify else "",
               "recent_comments": ctx.jira.recent_comments(ticket["key"], limit=10) if ctx.jira else []}
    context["owner_register"] = _owner_register(ctx, ticket)
    raised = escalations.open_escalation(ctx.db, ticket["key"])
    context["escalation_class"] = (raised or {}).get("class", "")
    _add_latest_owner_question(ctx, ticket, context)
    _add_prior_pr(ctx, ticket, context)
    return context


def _owner_register(ctx, ticket: dict) -> list[dict]:
    return [{"id": row["id"], "title": row["title"], "decision": row["decision"]}
            for row in OwnerDecisionsRepository(ctx.db).decided_register(ticket["project"])]


def _add_latest_owner_question(ctx, ticket: dict, context: dict) -> None:
    asked = EventsRepository(ctx.db).latest_ai_review_question(ticket["key"])
    prior_triage = RunsRepository(ctx.db).latest_triage_for_ticket(ticket["key"])
    if asked and (not prior_triage or asked["ts"] > prior_triage["started_at"]):
        question = str((asked.get("data") or {}).get("text") or "").strip()
        if question:
            context["last_dev_summary"] = f"Reviewer question: {question}"


def _add_prior_pr(ctx, ticket: dict, context: dict) -> None:
    prior_pr = RunsRepository(ctx.db).latest_pull_request_for_ticket(ticket["key"])
    match = re.search(r"/pull/(\d+)(?:/|$)", (prior_pr or {}).get("pr_url", ""))
    if match:
        context["pr"] = {"url": prior_pr["pr_url"], "number": int(match.group(1))}


def _claim_lease(ctx, ticket: dict, account, body, run_id: str):
    probation_id = None
    if ticket.get("probation"):
        probation_id = role_health.assign_probation(
            ctx.db, account.id, role_health.role_of(body.step) or "", ticket["key"])
        if probation_id is None:
            raise ClaimRejected("a probation ticket is already running")
    lease = ctx.leases.claim(ticket["key"], run_id, ctx.settings.lease_ms)
    if not lease.ok:
        if probation_id is not None:
            role_health.drop_probation(ctx.db, probation_id)
        raise ClaimRejected(f"held by {lease.holder}")
    return probation_id, lease


def _reserve_hotfiles(ctx, ticket: dict, body, run_id: str, fence: int, probation_id, ticket_epic: Callable) -> None:
    if body.step != "dev":
        return
    predicted = hotfiles.predict(ctx.db, ticket, ticket_epic(ticket))["hot"]
    taken = hotfiles.reserve(ctx.db, ticket["key"], run_id, predicted, slots=ctx.settings.hot_file_slots)
    if not taken:
        return
    _release_claim(ctx, ticket, run_id, fence, probation_id)
    raise ClaimRejected(f"hot file {', '.join(taken[1])} is reserved by {taken[0]}")


def _claim_state(ctx, body, node: str, ticket: dict, account, repo, triage_context: dict, run_id: str,
                 probation_id, fence: int, last_dev: Callable, ticket_epic: Callable) -> ClaimState:
    model, effort = repo.model_for(account.provider, "senior" if "tier-senior" in ticket["labels"] else account.tier)
    project = repo.projects.get(ticket["project"])
    epic = ticket_epic(ticket)
    ticket_data = {**{key: ticket[key] for key in
                      ("key", "project", "summary", "description", "labels", "priority", "type")},
                   "parent_key": ticket.get("parent_key"), "epic": epic}
    prior_dev = None if body.step == "master" else (last_dev(ticket["key"], done=True) or last_dev(ticket["key"]))
    selected_repo = project.settings_for_ticket(ticket_data) if project else {}
    selected_repo = _resume_repository(project, body.step, prior_dev, selected_repo)
    run_details = _repository_details(selected_repo)
    if body.step == "dev":
        run_details["capacity"] = _capacity_details(ctx, ticket, account)
    return ClaimState(body, node, ticket, account, repo, project, ticket_data, epic, prior_dev,
                      selected_repo, run_details, triage_context, run_id, probation_id, fence,
                      secrets.token_urlsafe(32), model, effort)


def _resume_repository(project, step: str, prior_dev, selected_repo: dict) -> dict:
    prior_repository = str(((prior_dev or {}).get("details") or {}).get("repository") or "")
    if not project or not prior_repository or step not in {"dev", "verify", "qa", "triage", "post_merge"}:
        return selected_repo
    prior_settings = ((prior_dev or {}).get("details") or {}).get("repository_settings")
    return dict(prior_settings) if isinstance(prior_settings, dict) else project.settings_for_repo(prior_repository)


def _repository_details(selected_repo: dict) -> dict:
    if not selected_repo:
        return {}
    return {"repository": selected_repo.get("repo", ""), "repository_settings": selected_repo}


def _capacity_details(ctx, ticket: dict, account) -> dict:
    estimate = capacity.estimate(ticket, capacity.history(ctx.db))
    rows = AccountsRepository(ctx.db).quota_forecasts()
    forecasts = {str(row["id"]): row.get("quota_forecast") or {} for row in rows}
    fit = capacity.account_fit(account.id, forecasts.get(account.id, {}), estimate)
    return {"estimate": estimate, "account_fit": fit,
            "assignment_reason": _capacity_reason(account, ticket, estimate, fit),
            "assigned_at": datetime.now(timezone.utc).isoformat()}


def _capacity_reason(account, ticket: dict, estimate: dict, fit: dict) -> str:
    tokens, minutes = estimate["tokens"], estimate["time_minutes"]
    if fit["known"] and fit["fits"]:
        senior_work = ("tier-senior" in (ticket.get("labels") or [])
                       or estimate["size"] in {"L", "XL"} or ticket.get("priority") == "Highest")
        order = "senior, then middle, then junior" if senior_work else "middle, then senior, then junior"
        return (f"Assigned to {account.id} ({account.provider}, {account.tier}): estimated work is "
                f"{tokens['low']:,.0f}–{tokens['high']:,.0f} tokens and {minutes['low']:,.0f}–"
                f"{minutes['high']:,.0f} minutes; {fit['reason']}; tier preference for this ticket is {order}.")
    if fit["known"]:
        return (f"Assigned to {account.id} ({account.provider}, {account.tier}) because it remains eligible and no "
                f"quota-safe eligible account was identified. The estimate is {tokens['low']:,.0f}–"
                f"{tokens['high']:,.0f} tokens and {minutes['low']:,.0f}–{minutes['high']:,.0f} minutes; "
                f"{fit['reason']}.")
    return (f"Assigned to {account.id} ({account.provider}, {account.tier}) by the existing eligibility rules. "
            f"The estimate is {tokens['low']:,.0f}–{tokens['high']:,.0f} tokens and {minutes['low']:,.0f}–"
            f"{minutes['high']:,.0f} minutes; quota history is not reliable enough to compare eligible accounts.")


def _persist_claim(ctx, state: ClaimState) -> bool:
    account, body = state.account, state.body
    run = (state.run_id, state.ticket["key"], state.ticket["project"], body.step, account.id,
           account.provider, state.model, state.node, state.fence,
           hashlib.sha256(state.run_token.encode()).hexdigest())
    if not shared_accounts.claim_run(ctx.db, account, run):
        return False
    if state.run_details:
        RunsRepository(ctx.db).save_claim_details(state.run_id, state.run_details)
    _inherit_pull_request(ctx, state)
    _attach_probation(ctx, state)
    AccountsRepository(ctx.db).mark_working(account.id)
    ctx.events.emit("run.claimed", ticket=state.ticket["key"], project=state.ticket["project"],
                    step=body.step, run_id=state.run_id, fence=state.fence, account=account.id,
                    provider=account.provider, model=state.model, node=state.node)
    return True


def _inherit_pull_request(ctx, state: ClaimState) -> None:
    prior = state.prior_dev
    if state.body.step == "dev" and prior and prior.get("pr_url") and prior.get("branch"):
        RunsRepository(ctx.db).inherit_pull_request(state.run_id, prior)


def _attach_probation(ctx, state: ClaimState) -> None:
    if state.probation_id is None:
        return
    role_health.attach_probation_run(ctx.db, state.probation_id, state.run_id)
    ctx.events.emit("role.probation", ticket=state.ticket["key"], project=state.ticket["project"],
                    step=state.body.step, run_id=state.run_id, account=state.account.id, outcome="assigned",
                    reason=f"probation ticket for role {role_health.role_of(state.body.step)}")


def _release_claim(ctx, ticket: dict, run_id: str, fence: int, probation_id) -> None:
    ctx.leases.release(ticket["key"], run_id, fence)
    hotfiles.unreserve(ctx.db, run_id)
    if probation_id is not None:
        role_health.drop_probation(ctx.db, probation_id)


def _step_context(ctx, state: ClaimState) -> dict:
    body, ticket = state.body, state.ticket
    extra: dict[str, Any] = {}
    if body.step == "dev":
        _development_context(ctx, state, extra)
    if body.step == "verify" and state.prior_dev:
        extra["pr"] = _prior_pr_context(state.prior_dev)
    if body.step == "triage":
        extra.update(state.triage_context)
    if body.step == "post_merge":
        extra["post_merge_criteria"] = next(
            (item["criteria"] for item in environment_reports.pending_post_merge(
                ctx, ticket["project"], [ticket["key"]])), [])
    if body.step == "intake":
        _intake_context(ctx, ticket, extra)
    if body.step == "impact":
        extra["project_tickets"] = TicketsRepository(ctx.db).impact_context(ticket["project"], ticket["key"])
    if body.step == "master":
        extra["master_scope"] = "global"
    else:
        extra["related"] = _related_hits(ctx, ticket)
    extra["context_pack"] = _context_pack(ctx, state)
    return extra


def _development_context(ctx, state: ClaimState, extra: dict) -> None:
    ticket, prior_dev = state.ticket, state.prior_dev
    tooling.resolve_ticket(ctx, ticket["key"], state.node)
    node = NodesRepository(ctx.db).claim_tooling(state.node)
    extra["machine_tools"] = tooling.machine_tools(node)
    verify = RunsRepository(ctx.db).latest_rework_for_ticket(ticket["key"])
    if verify and (not prior_dev or verify["started_at"] > prior_dev["started_at"]):
        extra["findings"] = (verify["details"] or {}).get("findings") or verify["summary"]
    if prior_dev and prior_dev["branch"]:
        extra["branch"] = prior_dev["branch"]
    _development_answer(ctx, state, extra)


def _development_answer(ctx, state: ClaimState, extra: dict) -> None:
    ticket = state.ticket
    runs = RunsRepository(ctx.db)
    answer = runs.latest_answered_triage(ticket["key"])
    later_dev = runs.has_completed_development_after(ticket["key"], answer["started_at"]) if answer else False
    question = (runs.latest_blocked_development_before(ticket["key"], answer["started_at"])
                if answer and not later_dev else None)
    if answer and not later_dev:
        extra["triage_answer"] = (answer["details"] or {}).get("answer") or answer["summary"]
        extra["triage_question"] = (question or {}).get("summary") or ""
    _owner_answers(ctx, ticket, answer, extra)


def _owner_answers(ctx, ticket: dict, answer, extra: dict) -> None:
    automatic = escalations.pending_answer(ctx.db, ticket["key"])
    if automatic and (not extra.get("triage_answer") or not answer
                      or automatic["created_at"] > answer["started_at"]):
        extra["triage_answer"], extra["triage_question"] = automatic["answer"], automatic["question"]
    owner = escalations.owner_answered_on_ticket(ctx, ticket["key"])
    if owner:
        extra["triage_answer"], extra["triage_question"] = owner["answer"], owner["question"]


def _prior_pr_context(prior_dev: dict) -> dict:
    url = prior_dev["pr_url"]
    return {"url": url, "number": int(url.rstrip("/").split("/")[-1]), "branch": prior_dev["branch"],
            "head_sha": prior_dev["head_sha"], "dev_account": prior_dev["account_id"],
            "dev_summary": (prior_dev["summary"] or "") + handover.render(prior_dev.get("details"))}


def _intake_context(ctx, ticket: dict, extra: dict) -> None:
    try:
        extra["recent_comments"] = ctx.jira.recent_comments(ticket["key"], limit=10) if ctx.jira else []
    except Exception as exc:  # noqa: BLE001
        log.warning("comments of %s for intake failed: %s", ticket["key"], exc)


def _related_hits(ctx, ticket: dict) -> list[dict]:
    try:
        return memory.related(ctx.db, ticket["project"], ticket["summary"],
                              exclude_ticket=ticket["key"], limit=3)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory lookup for %s failed: %s", ticket["key"], exc)
        return []


def _context_pack(ctx, state: ClaimState) -> dict:
    ticket = state.ticket
    query = f"{ticket.get('summary', '')}\n{ticket.get('description', '')}"[:12_000]
    try:
        return knowledge.context_pack(ctx.db, project=ticket.get("project", ""), ticket=ticket["key"],
                                      epic=state.epic or "", query=query, run_id=state.run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge context pack for %s failed: %s", ticket["key"], type(exc).__name__)
        return {"project": ticket.get("project", ""), "ticket": ticket["key"], "items": []}


def _claim_response(ctx, state: ClaimState, extra: dict, bot_identity: Callable) -> dict:
    project_data = _project_data(state)
    return {**extra, "claimed": True, "step": state.body.step, "run_id": state.run_id, "fence": state.fence,
            "run_token": state.run_token, "lease_ms": ctx.settings.lease_ms, "model": state.model,
            "effort": state.effort, "provider": state.account.provider, "account": state.account.id,
            "identity": bot_identity(), "ticket": state.ticket_data, "project": project_data}


def _project_data(state: ClaimState) -> dict:
    if state.body.step == "master":
        return {"key": "", "repo": "", "base": "", "hotfix_base": "", "checks": [], "stack": "",
                "node": state.node, "work_minutes": {"target": 30, "warn": 45, "hard": 60},
                "merge": {"method": "squash", "auto": False, "require_human": True}}
    project = state.project
    if not project:
        return {}
    return {"key": project.key, **state.selected_repo, "hotfix_base": project.hotfix_base,
            "work_minutes": project.work_minutes(), "environment": _public_environment(state),
            "merge": _merge_policy(project)}


def _public_environment(state: ClaimState) -> list[dict]:
    if state.body.step not in ("intake", "dev", "verify", "post_merge"):
        return []
    return [item.public() for item in state.project.environment_items]


def _merge_policy(project) -> dict:
    policy = project.merge or {}
    return {"method": policy.get("method", "squash"), "auto": bool(policy.get("auto", True)),
            "require_human": bool(policy.get("require_human", False))}
