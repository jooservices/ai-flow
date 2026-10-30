"""`POST /v1/work/claim`: a worker takes one ticket step (lease, hot files, run row, and everything it needs to start).

Moved verbatim out of the control app (JAIF-482). The helpers that still live in the app arrive as `deps`; the body is
otherwise unchanged and stays one atomic claim."""
from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ... import (capacity, claim_project, environment as environment_reports, escalations, hotfiles, knowledge,
                 licensing, load_policy_api, role_health, rollout, tooling)
from ... import dev_history, triage_claim as triage_claim_service
from ....core import handover
from ..work.constants import utcnow
from ...db import J
from ...route_deps import h

log = logging.getLogger("aiflow4.control")


def claim(ctx, deps, body, node: str) -> Any:
    # Candidate lists can be stale when several workers poll together. A live lease proves this ticket
    # cannot be granted, so refuse it before rebuilding licence and eligibility state. New claims still
    # pass every gate below, and the atomic lease acquisition handles the race after this check.
    if body.step != "master" and ctx.leases.holder(body.ticket):
        account = ctx.repo().account(body.account)
        if account and not account.runs_on(node):
            raise HTTPException(403, f"account {body.account} does not run on {node}")
        if account:
            return JSONResponse({"claimed": False, "reason": "ticket is already claimed"}, status_code=409)
    load_capacity, blocked = load_policy_api.work_claim_capacity(ctx, node)
    if blocked:
        return blocked
    try:
        state = licensing.enforce_work_entitlement(ctx, body.account, node,
                                                    public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        return JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
    # a node outside the version skew policy gets no new work (docs/v4/fleet-upgrade.md)
    policy = rollout.node_compatible(ctx, node, deps.releases())
    if not policy["compatible"]:
        return JSONResponse({"claimed": False, "reason": f"version skew: {policy['reason']}"}, status_code=409)
    if body.step == "master":
        candidates = [deps.master_candidate(body.account, node)]
    else:
        candidates = deps.eligible(body.account, node, ticket_key=body.ticket, lease_checked=True, step_filter=body.step)
        offered = next((candidate for candidate in candidates
                        if candidate["key"] == body.ticket and candidate["step"] == body.step), None)
        if offered:
            try:
                licensing.enforce_project_entitlement(ctx, offered["project"],
                                                      public_key=ctx.settings.license_public_key, state=state)
            except licensing.LicenseError as exc:
                return JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
    tickets = {(t["key"], t["step"]): t for t in candidates if t}
    t = tickets.get((body.ticket, body.step))
    if not t or (body.step == "dev" and not triage_claim_service.development_eligible(ctx, t)):
        return JSONResponse({"claimed": False, "reason": "not eligible"}, status_code=409)
    owner_answer = None
    if body.step == "dev":
        authorized, owner_answer = triage_claim_service.authorize_development(ctx, t)
        if not authorized:
            return JSONResponse({"claimed": False, "reason": "waiting for a verified owner answer"},
                                status_code=409)
    repo = ctx.repo()
    acc = repo.account(body.account)
    triage_context = triage_claim_service.context(ctx, t) if body.step == "triage" else {}
    run_id = str(uuid.uuid4())
    probation_id = None
    if t.get("probation"):  # one easy ticket at a time for a switched-off role
        probation_id = role_health.assign_probation(ctx.db, acc.id, role_health.role_of(body.step) or "", t["key"])
        if probation_id is None:
            return JSONResponse({"claimed": False, "reason": "a probation ticket is already running"},
                                status_code=409)
    c = ctx.leases.claim(t["key"], run_id, ctx.settings.lease_ms)
    if not c.ok:
        if probation_id is not None:
            role_health.drop_probation(ctx.db, probation_id)
        return JSONResponse({"claimed": False, "reason": f"held by {c.holder}"}, status_code=409)
    if body.step == "dev":
        # The eligibility check above is a snapshot: reserve the hot files now, atomically with the claim, so
        # two simultaneous claims of different tickets cannot both start on the same hot file.
        taken = hotfiles.reserve(ctx.db, t["key"], run_id, hotfiles.predict(ctx.db, t, deps.ticket_epic(t))["hot"],
                                slots=ctx.settings.hot_file_slots)
        if taken:
            ctx.leases.release(t["key"], run_id, c.fence)
            if probation_id is not None:
                role_health.drop_probation(ctx.db, probation_id)
            return JSONResponse({"claimed": False, "reason": f"hot file {', '.join(taken[1])} is reserved by "
                                 f"{taken[0]}"}, status_code=409)
    model, effort = repo.model_for(acc.provider, "senior" if "tier-senior" in t["labels"] else acc.tier)
    run_token = secrets.token_urlsafe(32)
    p = repo.projects.get(t["project"])
    epic = deps.ticket_epic(t)
    ticket_data = {**{k: t[k] for k in ("key", "project", "summary", "description", "labels", "priority", "type")},
                   "parent_key": t.get("parent_key"), "epic": epic}
    prior_dev = None if body.step == "master" else dev_history.delivered_or_last(ctx.db, t["key"])
    selected_repo = p.settings_for_ticket(ticket_data) if p else {}
    # Once development starts, verify and rework keep the repository that produced the PR even if
    # the owner changes the epic mapping while that work waits for review.
    prior_repository = str(((prior_dev or {}).get("details") or {}).get("repository") or "")
    if p and prior_repository and body.step in {"dev", "verify", "qa", "triage", "post_merge"}:
        prior_settings = ((prior_dev or {}).get("details") or {}).get("repository_settings")
        selected_repo = dict(prior_settings) if isinstance(prior_settings, dict) else p.settings_for_repo(prior_repository)
    run_details = ({"repository": selected_repo.get("repo", ""),
                    "repository_settings": selected_repo} if selected_repo else {})
    if body.step == "dev":
        capacity_samples = deps.capacity_history()
        ticket_estimate = capacity.estimate(t, capacity_samples)
        account_row = ctx.db.one("SELECT id,quota_forecast FROM accounts WHERE id=%s", (acc.id,)) or {}
        account_forecasts = ({str(account_row["id"]): account_row.get("quota_forecast") or {}}
                             if account_row else {})
        account_forecasts = capacity.with_active_quota_reservations(ctx.db, account_forecasts)
        fit = capacity.account_fit(acc.id, account_forecasts.get(acc.id, {}), ticket_estimate)
        expected_tokens = ticket_estimate["tokens"]
        expected_minutes = ticket_estimate["time_minutes"]
        quota_reservation_tokens = (int(expected_tokens["high"])
                                    if acc.sessions > 1 and fit.get("left_tokens") is not None else None)
        if fit["known"] and fit["fits"]:
            senior_work = ("tier-senior" in (t.get("labels") or [])
                           or ticket_estimate["size"] in {"L", "XL"} or t.get("priority") == "Highest")
            tier_order = ("senior, then middle, then junior" if senior_work
                          else "middle, then senior, then junior")
            strength = f"tier preference for this ticket is {tier_order}"
            reason = (f"Assigned to {acc.id} ({acc.provider}, {acc.tier}): estimated work is "
                      f"{expected_tokens['low']:,.0f}–{expected_tokens['high']:,.0f} tokens and "
                      f"{expected_minutes['low']:,.0f}–{expected_minutes['high']:,.0f} minutes; "
                      f"{fit['reason']}; {strength}.")
        elif fit["known"]:
            reason = (f"Assigned to {acc.id} ({acc.provider}, {acc.tier}) because it remains eligible and no "
                      f"quota-safe eligible account was identified. The estimate is "
                      f"{expected_tokens['low']:,.0f}–{expected_tokens['high']:,.0f} tokens and "
                      f"{expected_minutes['low']:,.0f}–{expected_minutes['high']:,.0f} minutes; {fit['reason']}.")
        else:
            reason = (f"Assigned to {acc.id} ({acc.provider}, {acc.tier}) by the existing eligibility rules. "
                      f"The estimate is {expected_tokens['low']:,.0f}–{expected_tokens['high']:,.0f} tokens and "
                      f"{expected_minutes['low']:,.0f}–{expected_minutes['high']:,.0f} minutes; "
                      "quota history is not reliable enough to compare eligible accounts.")
        run_details["capacity"] = {"estimate": ticket_estimate, "account_fit": fit,
                                   "assignment_reason": reason, "assigned_at": utcnow().isoformat()}
    else:
        quota_reservation_tokens = None
    claim_values = (run_id, t["key"], t["project"], body.step, acc.id, acc.provider,
                    model, node, c.fence, h(run_token))
    if not load_policy_api.claim_run(ctx, acc, claim_values, load_capacity, run_details=run_details,
                                     quota_reservation_tokens=quota_reservation_tokens):
        ctx.leases.release(t["key"], run_id, c.fence)
        hotfiles.unreserve(ctx.db, run_id)
        return JSONResponse({"claimed": False, "reason": f"account {acc.id} is at its limit of {acc.sessions}"
                             " concurrent sessions or machine load capacity is full"}, status_code=409)
    ctx.provider_eligibility.record_assignment(t, acc, body.step)
    if run_details:
        ctx.db.run("UPDATE runs SET details=%s WHERE id=%s", (J(run_details), run_id))
    if body.step == "dev" and prior_dev and prior_dev.get("pr_url") and prior_dev.get("branch"):
        # A rework run carries the PR identity that the previous developer run recorded. The CI-log endpoint
        # uses this row to bind branch and SHA requests to this ticket's pull request. Preserve provenance so
        # role health does not treat a later failed run as the run that produced the PR.
        ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s, head_sha=%s, details=details || %s WHERE id=%s",
                   (prior_dev["pr_url"], prior_dev["branch"], prior_dev.get("head_sha") or "",
                    J({"pr_url_source": "inherited"}), run_id))
    if probation_id is not None:
        role_health.attach_probation_run(ctx.db, probation_id, run_id)
        ctx.events.emit("role.probation", ticket=t["key"], project=t["project"], step=body.step, run_id=run_id,
                        account=acc.id, outcome="assigned",
                        reason=f"probation ticket for role {role_health.role_of(body.step)}")
    ctx.db.run("UPDATE accounts SET status='working', updated_at=now(), idle_since=NULL, cooldown_since=NULL WHERE id=%s",
               (acc.id,))
    ctx.events.emit("run.claimed", ticket=t["key"], project=t["project"], step=body.step, run_id=run_id,
                    fence=c.fence, account=acc.id, provider=acc.provider, model=model, node=node)
    extra: dict[str, Any] = {}
    if body.step == "dev":
        tooling.resolve_ticket(ctx, t["key"], node)
        extra["machine_tools"] = tooling.machine_tools(
            ctx.db.one("SELECT tools,metrics FROM nodes WHERE name=%s", (node,)) or {})
    dev = prior_dev
    if body.step == "verify" and dev:
        extra["pr"] = {"url": dev["pr_url"], "number": int(dev["pr_url"].rstrip("/").split("/")[-1]),
                       "branch": dev["branch"], "head_sha": dev["head_sha"], "dev_account": dev["account_id"],
                       "dev_summary": (dev["summary"] or "") + handover.render(dev.get("details"))}
    if body.step == "dev":
        v = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s"
                       " AND step IN ('verify','qa')"
                       " AND outcome='rework' ORDER BY started_at DESC LIMIT 1", (t["key"],))
        if v and (not dev or v["started_at"] > dev["started_at"]):
            extra["findings"] = (v["details"] or {}).get("findings") or v["summary"]
        if dev and dev["branch"]:
            extra["branch"] = dev["branch"]
        # the master AI's answer to the question a dev run stopped on, until a dev run finishes or blocks again
        a = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                       " AND outcome='answered' ORDER BY started_at DESC LIMIT 1", (t["key"],))
        if a and not ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND step='dev' AND started_at > %s"
                                " AND outcome IN ('done', 'blocked') LIMIT 1", (t["key"], a["started_at"])):
            q = ctx.db.one("SELECT summary FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='blocked'"
                           " AND started_at < %s ORDER BY started_at DESC LIMIT 1", (t["key"], a["started_at"]))
            extra["triage_answer"] = (a["details"] or {}).get("answer") or a["summary"]
            extra["triage_question"] = (q or {}).get("summary") or ""
        # an escalation resolved without a triage run (register, policy message, machine, amendment) is answered
        # the same way; the newer of the two wins
        auto = escalations.pending_answer(ctx.db, t["key"])
        if auto and (not extra.get("triage_answer") or not a or auto["created_at"] > a["started_at"]):
            extra["triage_answer"], extra["triage_question"] = auto["answer"], auto["question"]
        if owner_answer:
            extra["triage_answer"] = owner_answer["answer"]
            extra["triage_question"] = owner_answer["question"]
    if body.step == "triage":
        extra.update(triage_context)
    if body.step == "post_merge":
        extra["post_merge_criteria"] = next(
            (item["criteria"] for item in environment_reports.pending_post_merge(ctx, t["project"], [t["key"]])), [])
    if body.step == "intake":
        # owners answer intake questions in comments as often as in the description
        try:
            extra["recent_comments"] = ctx.jira.recent_comments(t["key"], limit=10) if ctx.jira else []
        except Exception as e:  # noqa: BLE001
            log.warning("comments of %s for intake failed: %s", t["key"], e)
    if body.step == "impact":
        # the board the assessment compares against: the project's epics and its open work
        extra["project_tickets"] = ctx.db.all(
            "SELECT key, type, status, summary, parent_key FROM tickets WHERE project=%s AND status<>'Done'"
            " AND key<>%s ORDER BY type<>'Epic', key LIMIT 300", (t["project"], t["key"]))
    if body.step == "master":
        extra["master_scope"] = "global"
    else:
        extra["related"] = deps.related_hits(t)
    try:
        ticket_context = f"{t.get('summary', '')}\n{t.get('description', '')}"[:12_000]
        extra["context_pack"] = knowledge.context_pack(ctx.db, project=t.get("project", ""), ticket=t["key"],
                                                       epic=epic or "", query=ticket_context, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - a local search failure cannot prevent a lease
        log.warning("knowledge context pack for %s failed: %s", t["key"], type(exc).__name__)
        extra["context_pack"] = {"project": t.get("project", ""), "ticket": t["key"], "items": []}
    project_data = claim_project.payload(p, selected_repo, node, body.step, ctx.db, t["key"])
    return {**extra, "claimed": True, "step": body.step, "run_id": run_id, "fence": c.fence, "run_token": run_token,
            "lease_ms": ctx.settings.lease_ms, "model": model, "effort": effort, "provider": acc.provider,
            "account": acc.id, "identity": deps.bot_identity(),
            "ticket": ticket_data,
            "project": project_data}
