"""Claim a worker run after the route has authenticated its node."""
from __future__ import annotations

import logging
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from . import (capacity, environment as environment_reports, escalations, hotfiles, knowledge,
               licensing, role_health, rollout, shared_accounts, tooling)
from .db import J
from .repositories import developer_runs as developer_run_repository
from ..core import handover
from ..core.work_limits import retry_hard_minutes

log = logging.getLogger("aiflow4.work_claim")


@dataclass(frozen=True)
class ClaimHelpers:
    eligible: Callable
    master_candidate: Callable
    last_dev: Callable
    ticket_epic: Callable
    related_hits: Callable
    bot_identity: Callable


def claim(ctx, body, node: str, helpers: ClaimHelpers, releases) -> JSONResponse | dict:
    """Coordinate eligibility, fenced reservation, run context, and the response sent to a worker."""
    ticket, denied = _eligible_ticket(ctx, body, node, helpers, releases)
    if denied:
        return denied
    triage = _triage_context(ctx, body, ticket, helpers) if body.step == "triage" else {}
    reservation, denied = _reserve(ctx, body, ticket, helpers)
    if denied:
        return denied
    data = _claim_data(ctx, body, node, ticket, reservation, helpers)
    denied = _persist_claim(ctx, body, node, ticket, reservation, data)
    if denied:
        return denied
    extra = _extra_context(ctx, body, ticket, reservation, data, triage, helpers)
    return _response(ctx, body, node, ticket, reservation, data, extra, helpers)


def _eligible_ticket(ctx, body, node: str, helpers: ClaimHelpers, releases):
    try:
        state = licensing.enforce_work_entitlement(ctx, body.account, node,
                                                   public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        return None, JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
    policy = rollout.node_compatible(ctx, node, releases)
    if not policy["compatible"]:
        return None, JSONResponse({"claimed": False, "reason": f"version skew: {policy['reason']}"}, status_code=409)
    candidates = ([helpers.master_candidate(body.account, node)] if body.step == "master"
                  else helpers.eligible(body.account, node))
    offered = next((candidate for candidate in candidates if candidate and candidate["key"] == body.ticket
                    and candidate["step"] == body.step), None)
    if offered:
        try:
            licensing.enforce_project_entitlement(ctx, offered["project"],
                                                  public_key=ctx.settings.license_public_key, state=state)
        except licensing.LicenseError as exc:
            return None, JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
    ticket = next((candidate for candidate in candidates if candidate and candidate["key"] == body.ticket
                   and candidate["step"] == body.step), None)
    if not ticket:
        return None, JSONResponse({"claimed": False, "reason": "not eligible"}, status_code=409)
    return ticket, None


def _triage_context(ctx, body, ticket: dict, helpers: ClaimHelpers) -> dict:
    verify = ctx.db.one("SELECT summary, details FROM runs WHERE ticket_key=%s AND step='verify'"
                        " AND status<>'running' ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
    comments = ctx.jira.recent_comments(ticket["key"], limit=10) if ctx.jira else []
    data = {"last_dev_summary": (helpers.last_dev(ticket["key"]) or {}).get("summary", ""),
            "verify_findings": ((verify["details"] or {}).get("findings") or verify["summary"])
            if verify else "", "recent_comments": comments}
    data["owner_register"] = [
        {"id": row["id"], "title": row["title"], "decision": row["decision"]} for row in ctx.db.all(
            "SELECT id,title,decision FROM owner_decisions WHERE kind='decision' AND status='decided'"
            " AND (project='' OR project=%s) ORDER BY id LIMIT 60", (ticket["project"],))]
    raised = escalations.open_escalation(ctx.db, ticket["key"])
    data["escalation_class"] = (raised or {}).get("class", "")
    _triage_question_context(ctx, ticket, data)
    return data


def _triage_question_context(ctx, ticket: dict, data: dict) -> None:
    asked = ctx.db.one("SELECT ts,data FROM events WHERE ticket=%s AND type='review.action'"
                       " AND data->>'action'='ask-ai' ORDER BY ts DESC LIMIT 1", (ticket["key"],))
    prior = ctx.db.one("SELECT started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                       " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
    if asked and (not prior or asked["ts"] > prior["started_at"]):
        question = str((asked.get("data") or {}).get("text") or "").strip()
        if question:
            data["last_dev_summary"] = f"Reviewer question: {question}"
    prior_pr = ctx.db.one("SELECT pr_url FROM runs WHERE ticket_key=%s AND pr_url<>''"
                          " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
    match = re.search(r"/pull/(\d+)(?:/|$)", (prior_pr or {}).get("pr_url", ""))
    if match:
        data["pr"] = {"url": prior_pr["pr_url"], "number": int(match.group(1))}


def _reserve(ctx, body, ticket: dict, helpers: ClaimHelpers):
    run_id = str(uuid.uuid4())
    probation_id = None
    if ticket.get("probation"):
        account = ctx.repo().account(body.account)
        probation_id = role_health.assign_probation(ctx.db, account.id,
                                                     role_health.role_of(body.step) or "", ticket["key"])
        if probation_id is None:
            return None, JSONResponse({"claimed": False, "reason": "a probation ticket is already running"},
                                      status_code=409)
    lease = ctx.leases.claim(ticket["key"], run_id, ctx.settings.lease_ms)
    if not lease.ok:
        if probation_id is not None:
            role_health.drop_probation(ctx.db, probation_id)
        return None, JSONResponse({"claimed": False, "reason": f"held by {lease.holder}"}, status_code=409)
    if body.step == "dev":
        taken = hotfiles.reserve(ctx.db, ticket["key"], run_id,
                                 hotfiles.predict(ctx.db, ticket, helpers.ticket_epic(ticket))["hot"],
                                 slots=ctx.settings.hot_file_slots)
        if taken:
            ctx.leases.release(ticket["key"], run_id, lease.fence)
            if probation_id is not None:
                role_health.drop_probation(ctx.db, probation_id)
            reason = f"hot file {', '.join(taken[1])} is reserved by {taken[0]}"
            return None, JSONResponse({"claimed": False, "reason": reason}, status_code=409)
    return {"run_id": run_id, "fence": lease.fence, "probation_id": probation_id}, None


def _claim_data(ctx, body, node: str, ticket: dict, reservation: dict, helpers: ClaimHelpers) -> dict:
    repo = ctx.repo()
    account = repo.account(body.account)
    model, effort = repo.model_for(account.provider, "senior" if "tier-senior" in ticket["labels"] else account.tier)
    run_token = secrets.token_urlsafe(32)
    project = repo.projects.get(ticket["project"])
    epic = helpers.ticket_epic(ticket)
    ticket_data = {**{key: ticket[key] for key in ("key", "project", "summary", "description", "labels", "priority", "type")},
                   "parent_key": ticket.get("parent_key"), "epic": epic}
    prior_dev = None if body.step == "master" else helpers.last_dev(ticket["key"], done=True) or helpers.last_dev(ticket["key"])
    selected_repo = project.settings_for_ticket(ticket_data) if project else {}
    if project and prior_dev and (prior_dev.get("details") or {}).get("repository") and body.step in {
            "dev", "verify", "qa", "triage", "post_merge"}:
        details = prior_dev["details"] or {}
        settings = details.get("repository_settings")
        selected_repo = dict(settings) if isinstance(settings, dict) else project.settings_for_repo(details["repository"])
    data = {"repo": repo, "account": account, "project": project, "model": model, "effort": effort,
            "run_id": reservation["run_id"], "run_token": run_token, "epic": epic, "ticket_data": ticket_data,
            "prior_dev": prior_dev, "selected_repo": selected_repo, "run_details": {},
            "node": node,
            "limits": project.work_minutes() if project else {"target": 30, "warn": 45, "hard": 60},
            "timeout_retries": 0, "intake_estimate": None}
    if selected_repo:
        data["run_details"].update(repository=selected_repo.get("repo", ""), repository_settings=selected_repo)
    _estimate_claim(ctx, body, ticket, data)
    return data


def _estimate_claim(ctx, body, ticket: dict, data: dict) -> None:
    if body.step == "intake":
        data["intake_estimate"] = capacity.estimate(ticket, capacity.history(ctx.db))
        data["run_details"]["intake_estimate"] = data["intake_estimate"]
    if body.step != "dev":
        return
    retries = developer_run_repository.timeout_count(ctx.db, ticket["key"])
    data["timeout_retries"] = retries
    if retries:
        data["limits"]["hard"] = retry_hard_minutes(data["limits"]["hard"], retries)
        data["run_details"]["timeout_retry"] = {"previous_timeouts": retries,
                                                  "hard_minutes": data["limits"]["hard"]}
    data["run_details"]["effective_work_minutes"] = dict(data["limits"])
    _capacity_reason(ctx, ticket, data)


def _capacity_reason(ctx, ticket: dict, data: dict) -> None:
    estimate = capacity.estimate(ticket, capacity.history(ctx.db))
    forecasts = {str(row["id"]): row.get("quota_forecast") or {} for row in
                 ctx.db.all("SELECT id,quota_forecast FROM accounts ORDER BY id")}
    fit = capacity.account_fit(data["account"].id, forecasts.get(data["account"].id, {}), estimate)
    reason = capacity.assignment_reason(data["account"], ticket, estimate, fit)
    data["run_details"]["capacity"] = {"estimate": estimate, "account_fit": fit, "assignment_reason": reason,
                                        "assigned_at": datetime.now(timezone.utc).isoformat()}


def _persist_claim(ctx, body, node: str, ticket: dict, reservation: dict, data: dict):
    run_token = data["run_token"]
    args = (data["run_id"], ticket["key"], ticket["project"], body.step, data["account"].id,
            data["account"].provider, data["model"], node, reservation["fence"],
            hashlib.sha256(run_token.encode()).hexdigest())
    if not shared_accounts.claim_run(ctx.db, data["account"], args):
        ctx.leases.release(ticket["key"], data["run_id"], reservation["fence"])
        hotfiles.unreserve(ctx.db, data["run_id"])
        reason = f"account {data['account'].id} is at its limit of {data['account'].sessions} concurrent sessions"
        return JSONResponse({"claimed": False, "reason": reason}, status_code=409)
    if data["run_details"]:
        ctx.db.run("UPDATE runs SET details=%s WHERE id=%s", (J(data["run_details"]), data["run_id"]))
    _inherit_pull_request(ctx, body, ticket, data)
    _record_probation(ctx, body, ticket, reservation, data)
    ctx.db.run("UPDATE accounts SET status='working', updated_at=now(), idle_since=NULL, cooldown_since=NULL WHERE id=%s",
               (data["account"].id,))
    ctx.events.emit("run.claimed", ticket=ticket["key"], project=ticket["project"], step=body.step,
                    run_id=data["run_id"], fence=reservation["fence"], account=data["account"].id,
                    provider=data["account"].provider, model=data["model"], node=node)
    return None


def _inherit_pull_request(ctx, body, ticket: dict, data: dict) -> None:
    prior = data["prior_dev"]
    if body.step == "dev" and prior and prior.get("pr_url") and prior.get("branch"):
        ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s, head_sha=%s, details=details || %s WHERE id=%s",
                   (prior["pr_url"], prior["branch"], prior.get("head_sha") or "",
                    J({"pr_url_source": "inherited"}), data["run_id"]))


def _record_probation(ctx, body, ticket: dict, reservation: dict, data: dict) -> None:
    probation_id = reservation["probation_id"]
    if probation_id is None:
        return
    role_health.attach_probation_run(ctx.db, probation_id, data["run_id"])
    ctx.events.emit("role.probation", ticket=ticket["key"], project=ticket["project"], step=body.step,
                    run_id=data["run_id"], account=data["account"].id, outcome="assigned",
                    reason=f"probation ticket for role {role_health.role_of(body.step)}")


def _extra_context(ctx, body, ticket: dict, reservation: dict, data: dict, triage: dict,
                   helpers: ClaimHelpers) -> dict:
    extra: dict = {}
    if body.step == "dev":
        _dev_context(ctx, ticket, data, extra)
    if body.step == "verify" and data["prior_dev"]:
        extra["pr"] = _pull_request_context(data["prior_dev"])
    if body.step == "triage":
        extra.update(triage)
    if body.step == "post_merge":
        extra["post_merge_criteria"] = next(
            (item["criteria"] for item in environment_reports.pending_post_merge(ctx, ticket["project"],
                                                                                  [ticket["key"]])), [])
    if body.step == "intake":
        try:
            extra["recent_comments"] = ctx.jira.recent_comments(ticket["key"], limit=10) if ctx.jira else []
        except Exception as exc:  # noqa: BLE001
            log.warning("comments of %s for intake failed: %s", ticket["key"], exc)
    if body.step == "impact":
        extra["project_tickets"] = ctx.db.all(
            "SELECT key, type, status, summary, parent_key FROM tickets WHERE project=%s AND status<>'Done'"
            " AND key<>%s ORDER BY type<>'Epic', key LIMIT 300", (ticket["project"], ticket["key"]))
    if body.step == "master":
        extra["master_scope"] = "global"
    else:
        extra["related"] = helpers.related_hits(ticket)
    try:
        context = f"{ticket.get('summary', '')}\n{ticket.get('description', '')}"[:12_000]
        extra["context_pack"] = knowledge.context_pack(ctx.db, project=ticket.get("project", ""),
                                                       ticket=ticket["key"], epic=data["epic"] or "",
                                                       query=context, run_id=data["run_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge context pack for %s failed: %s", ticket["key"], type(exc).__name__)
        extra["context_pack"] = {"project": ticket.get("project", ""), "ticket": ticket["key"], "items": []}
    return extra


def _dev_context(ctx, ticket: dict, data: dict, extra: dict) -> None:
    tooling.resolve_ticket(ctx, ticket["key"], data["node"])
    machine = ctx.db.one("SELECT tools,metrics FROM nodes WHERE name=%s", (data["node"],)) or {}
    extra["machine_tools"] = tooling.machine_tools(machine)
    v = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s AND step IN ('verify','qa')"
                   " AND outcome='rework' ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
    dev = data["prior_dev"]
    if v and (not dev or v["started_at"] > dev["started_at"]):
        extra["findings"] = (v["details"] or {}).get("findings") or v["summary"]
    if dev and dev.get("branch"):
        extra["branch"] = dev["branch"]
    _triage_answer(ctx, ticket, extra)


def _triage_answer(ctx, ticket: dict, extra: dict) -> None:
    answer = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                        " AND outcome='answered' ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
    if answer and not ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND step='dev' AND started_at > %s"
                                 " AND outcome IN ('done', 'blocked') LIMIT 1", (ticket["key"], answer["started_at"])):
        question = ctx.db.one("SELECT summary FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='blocked'"
                              " AND started_at < %s ORDER BY started_at DESC LIMIT 1",
                              (ticket["key"], answer["started_at"]))
        extra["triage_answer"] = (answer["details"] or {}).get("answer") or answer["summary"]
        extra["triage_question"] = (question or {}).get("summary") or ""
    pending = escalations.pending_answer(ctx.db, ticket["key"])
    if pending and (not extra.get("triage_answer") or not answer
                    or pending["created_at"] > answer["started_at"]):
        extra["triage_answer"], extra["triage_question"] = pending["answer"], pending["question"]
    owner = escalations.owner_answered_on_ticket(ctx, ticket["key"])
    if owner:
        extra["triage_answer"], extra["triage_question"] = owner["answer"], owner["question"]


def _pull_request_context(dev: dict) -> dict:
    url = dev["pr_url"]
    return {"url": url, "number": int(url.rstrip("/").split("/")[-1]), "branch": dev["branch"],
            "head_sha": dev["head_sha"], "dev_account": dev["account_id"],
            "dev_summary": (dev["summary"] or "") + handover.render(dev.get("details"))}


def _response(ctx, body, node: str, ticket: dict, reservation: dict, data: dict,
              extra: dict, helpers: ClaimHelpers) -> dict:
    project = data["project"]
    if data["intake_estimate"]:
        data["ticket_data"]["work_estimate"] = {**data["intake_estimate"], "target_minutes": 30}
    project_data = _project_response(body, node, data)
    if body.step == "dev":
        project_data["timeout_retries"] = data["timeout_retries"]
    return {**extra, "claimed": True, "step": body.step, "run_id": data["run_id"], "fence": reservation["fence"],
            "run_token": data["run_token"], "lease_ms": ctx.settings.lease_ms, "model": data["model"],
            "effort": data["effort"], "provider": data["account"].provider, "account": data["account"].id,
            "identity": helpers.bot_identity(), "ticket": data["ticket_data"], "project": project_data}


def _project_response(body, node: str, data: dict) -> dict:
    project = data["project"]
    if body.step == "master":
        return {"key": "", "repo": "", "base": "", "hotfix_base": "", "checks": [], "stack": "", "node": node,
                "work_minutes": {"target": 30, "warn": 45, "hard": 60},
                "merge": {"method": "squash", "auto": False, "require_human": True}}
    if not project:
        return {}
    settings = data["selected_repo"]
    return {"key": project.key, **settings, "hotfix_base": project.hotfix_base,
            "work_minutes": data["limits"],
            "environment": ([item.public() for item in project.environment_items]
                            if body.step in ("intake", "dev", "verify", "post_merge") else []),
            "merge": {"method": (project.merge or {}).get("method", "squash"),
                      "auto": bool((project.merge or {}).get("auto", True)),
                      "require_human": bool((project.merge or {}).get("require_human", False))}}


