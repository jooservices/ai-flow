"""Routes of the runs feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from datetime import timedelta
from types import SimpleNamespace

from .bodies import FenceBody, MasterActionBody, MasterBudgetBody, MasterFlowBugsBody, MasterPlanOutcomeBody, ResultBody
from typing import Any
from fastapi import Depends, HTTPException, Query, Request
from ... import (knowledge, load_policy, quota_refresh, role_health, support_bundle, tooling)
from ... import run_inbox as run_inbox_control
from ... import environment as environment_reports
from ...db import J
from ...features.work.constants import utcnow
from ...features.work.listeners import run_finished
from . import autonomy, plan_outcome
from .events import MasterActionCompleted, MasterDailyBudgetUpdated
from ...platform.events.wiring import publish_for
from .master_actions import (MasterActionState, decide, deny_policy_decision, escalate, master_action_event,
                             perform_action, require_intent, validate_ticket_action)


def _prepare_run_result(ctx, run, body, node, still_owner):
    details = dict(body.details)
    details.pop("pr_url_source", None)  # provenance is set by the control API, never trusted from the worker
    details.pop("quota_reservation", None)  # quota reservations are set by the control API
    if body.pr_url:
        details["pr_url_source"] = "current"
    if still_owner and run["step"] == "verify" and body.outcome in ("merged", "human"):
        try:
            details["post_merge_criteria"] = environment_reports.record_post_merge_criteria(
                ctx, run, details.get("post_merge_criteria") or [])
        except environment_reports.UnmappedPostMergeCriteria as exc:
            raise HTTPException(422, str(exc)) from exc
    run_log = details.pop("support_log_text", None)  # raw logs never reach the runs table or its backups
    details.pop("support_log_truncated", None)
    outcome, error_kind = body.outcome, body.error_kind
    if still_owner:
        outcome, error_kind, details = load_policy.result_after_load_failure(
            ctx, node, body, details, utcnow().isoformat())
        body.outcome = outcome
        body.error_kind = error_kind
    return details, run_log, outcome, error_kind


def _finish_run_result(ctx, run, run_id, body, details, node, still_owner):
    late_result = run_inbox_control.finish(ctx, run, run_id, body, details, node)
    account_update_allowed = not late_result and still_owner
    status_update_allowed = account_update_allowed and run_inbox_control.account_update_allowed(
        ctx, run_id, run["account_id"], still_owner)
    return late_result, account_update_allowed, status_update_allowed


def _update_run_result_account(ctx, run, run_id, node, body, run_log, late_result,
                               account_update_allowed, status_update_allowed, refresh_quota_forecast,
                               mark_machine_auth_failure, log):
    if not late_result and isinstance(run_log, str) and run_log:
        try:
            support_bundle.store_run_log(ctx, run_id, run_log)
        except Exception as exc:  # noqa: BLE001 - a missing diagnostic log must not lose the run result
            log.warning("support log for run %s not stored: %s", run_id, type(exc).__name__)
    if body.quota:
        quota_refresh.record_run_result(ctx.db, run, body, update_account=account_update_allowed)
    elif account_update_allowed:
        current = ctx.db.one("SELECT quota FROM accounts WHERE id=%s", (run["account_id"],)) or {}
        refresh_quota_forecast(run["account_id"], run["provider"], current.get("quota") or {})
    if account_update_allowed and body.error_kind == "auth":
        mark_machine_auth_failure(run["account_id"], node, body.summary)
    if status_update_allowed:
        if body.error_kind == "quota":
            until = body.reset_at or (utcnow() + timedelta(minutes=30))
            seconds = max(60, int((until - utcnow()).total_seconds()))
            changed = ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s, updated_at=now(),"
                                 " cooldown_since=coalesce(cooldown_since,now()), idle_since=NULL WHERE id=%s"
                                 " AND NOT EXISTS (SELECT 1 FROM runs WHERE account_id=%s AND status='running')",
                                 ("cooldown", until, body.summary[:300], run["account_id"], run["account_id"]))
            if changed:
                ctx.leases.cooldown(run["account_id"], seconds, "quota")
        elif body.error_kind != "auth":
            ctx.db.run("UPDATE accounts SET status='idle', last_error=CASE WHEN %s IN ('failed','timeout') THEN %s ELSE '' END,"
                       " updated_at=now(), idle_since=coalesce(idle_since,now()), cooldown_since=NULL"
                       " WHERE id=%s AND status='working' AND NOT EXISTS"
                       " (SELECT 1 FROM runs WHERE account_id=%s AND status='running')",
                       (body.outcome, body.summary[:300], run["account_id"], run["account_id"]))


def _record_run_result_missing(ctx, run, run_id, body, details, late_result, outcome):
    if not late_result and body.outcome == "tool_missing" and run["step"] == "dev":
        recorded = tooling.record_missing(ctx, run, [str(x) for x in body.details.get("missing_tools") or []],
                                          body.summary)
        details["missing_tools"] = recorded
        if not recorded:
            outcome = "failed"
            ctx.db.run("UPDATE runs SET outcome='failed',error_kind=coalesce(nullif(error_kind,''),%s),details=details || %s"
                       " WHERE id=%s",
                       ("missing_requirement_unaccepted", J({"missing_tools": [], "missing_requirement_unaccepted": True}),
                        run_id))
        else:
            ctx.db.run("UPDATE runs SET details=details || %s WHERE id=%s", (J({"missing_tools": recorded}), run_id))
    if not late_result and body.outcome == "environment_missing" and run["step"] == "dev":
        recorded = environment_reports.record_missing(
            ctx, run, [str(x) for x in body.details.get("missing_environment") or []], body.summary)
        ctx.db.run("UPDATE runs SET details=details || %s WHERE id=%s",
                   (J({"missing_environment": recorded}), run_id))
        if not recorded:  # a name that is not in the project configuration is not an environment item
            outcome = "failed"
            ctx.db.run("UPDATE runs SET outcome='failed', error_kind=coalesce(nullif(error_kind,''),"
                       "'missing_environment_unaccepted') WHERE id=%s", (run_id,))
    return outcome


def _capture_run_result_followups(ctx, run, run_id, body, late_result, last_dev, log):
    if not late_result:
        try:
            knowledge.capture_run(ctx.db, run_id, ctx.settings.jira_site)
        except Exception as exc:  # noqa: BLE001 - per-run capture cannot undo a completed worker run
            log.warning("knowledge capture for run %s failed: %s", run_id, type(exc).__name__)
    dur = int((utcnow() - run["started_at"]).total_seconds() * 1000)
    if ctx.jira and run["step"] == "dev" and not late_result and body.outcome not in ("lost",):
        try:  # developer time on the ticket (each dev run, rework included); the detailed timeline is in OpenObserve
            ctx.jira.add_worklog(run["ticket_key"], max(60, dur // 1000), run["started_at"],
                                 f"AI Flow {run['step']} by {run['account_id']} ({run['provider']}"
                                 f"{'/' + run['model'] if run['model'] else ''}): {body.outcome}."
                                 f" Run {run_id}, fence {body.fence}.")
        except Exception as e:  # noqa: BLE001
            log.warning("worklog for %s failed: %s", run["ticket_key"], e)
    return dur


def _evaluate_run_result_health(ctx, run, run_id, late_result, last_dev, log):
    try:  # quality by role: this result may switch the role off or settle a probation ticket
        if not late_result:
            role_health.evaluate(ctx, run["account_id"])
            if run["step"] == "verify":  # a verdict also decides the dev run it judged
                dev = last_dev(run["ticket_key"])
                if dev and dev["account_id"] != run["account_id"]:
                    role_health.evaluate(ctx, dev["account_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("role health after run %s failed: %s", run_id, e)


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module
    runs = _install_run_routes(app, ctx, deps, log)
    master = _install_master_routes(app, ctx, deps, log)
    return SimpleNamespace(run_heartbeat=runs.run_heartbeat, run_result=runs.run_result, run_release=runs.run_release,
                           master_context=master.master_context, master_flow_bugs=master.master_flow_bugs,
                           master_plan_outcome=master.master_plan_outcome, autonomy_report=master.autonomy_report,
                           master_autonomy=master.master_autonomy, master_budget_update=master.master_budget_update,
                           master_report=master.master_report, master_action=master.master_action)


def _install_run_routes(app, ctx, deps, log) -> SimpleNamespace:
    last_dev = deps.last_dev
    mark_machine_auth_failure = deps.mark_machine_auth_failure
    node_auth = deps.node_auth
    refresh_quota_forecast = deps.refresh_quota_forecast
    run_row = deps.run_row
    # --- runs -----------------------------------------------------------------------------------------------
    @app.post("/v1/runs/{run_id}/heartbeat")
    def run_heartbeat(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        return run_inbox_control.heartbeat(ctx, run_row(run_id), run_id, body, node)

    @app.post("/v1/runs/{run_id}/result")
    def run_result(run_id: str, body: ResultBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        if int(run["fence"]) != body.fence:
            raise HTTPException(409, "run fence is no longer active")
        still_owner = ctx.leases.valid(run["ticket_key"], run_id, body.fence)
        details, run_log, outcome, error_kind = _prepare_run_result(ctx, run, body, node, still_owner)
        late_result, account_update_allowed, status_update_allowed = _finish_run_result(
            ctx, run, run_id, body, details, node, still_owner)
        if late_result:
            outcome = "lost"
        _update_run_result_account(ctx, run, run_id, node, body, run_log, late_result,
                                   account_update_allowed, status_update_allowed, refresh_quota_forecast,
                                   mark_machine_auth_failure, log)
        outcome = _record_run_result_missing(ctx, run, run_id, body, details, late_result, outcome)
        dur = _capture_run_result_followups(ctx, run, run_id, body, late_result, last_dev, log)
        run_finished(ctx, run_id=run_id, ticket_key=run["ticket_key"], project=run["project"], step=run["step"],
                     fence=body.fence, account=run["account_id"], provider=run["provider"], model=run["model"], node=node,
                     duration_ms=dur, outcome=outcome, reason=error_kind or body.summary[:200], usage_in=body.tokens_in,
                     usage_out=body.tokens_out, cost_usd=body.cost_usd, still_owner=still_owner, pr_url=body.pr_url)
        _evaluate_run_result_health(ctx, run, run_id, late_result, last_dev, log)
        return {"ok": True, "still_owner": still_owner}

    @app.post("/v1/runs/{run_id}/release")
    def run_release(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        released = run_inbox_control.release(ctx, run, run_id, body, node)
        ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'", (run["account_id"],))
        ctx.events.emit("run.released", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="released" if released else "not_owner")
        return {"released": released}

    return SimpleNamespace(run_heartbeat=run_heartbeat, run_result=run_result, run_release=run_release)


def _install_master_routes(app, ctx, deps, log) -> SimpleNamespace:
    audit = deps.audit
    console_user = deps.console_user
    master_budget_state = deps.master_budget_state
    master_run = deps.master_run
    master_snapshot = deps.master_snapshot
    node_auth = deps.node_auth
    process_flow_bugs = deps.process_flow_bugs
    @app.get("/v1/master/context")
    def master_context(run_id: str, fence: int, node: str = Depends(node_auth)) -> dict:
        master_run(run_id, fence, node, "context")
        return master_snapshot()

    @app.post("/v1/master/flow-bugs")
    def master_flow_bugs(body: MasterFlowBugsBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, "flow-bug-scan")
        return {"reports": process_flow_bugs(run)}

    @app.post("/v1/master/plan-outcome")
    def master_plan_outcome(body: MasterPlanOutcomeBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, "plan-outcome", {"valid": body.valid})
        return plan_outcome.record_plan_outcome(ctx, run, body, node)

    def autonomy_report(weeks: int = 8, scope: tuple[str, ...] | None = None) -> dict[str, Any]:
        return autonomy.autonomy_report(ctx, weeks, scope)

    @app.get("/v1/master/autonomy")
    def master_autonomy(request: Request) -> dict:
        user = console_user(request, "console.read")
        return {"budget": master_budget_state(), "report": autonomy_report(scope=user.project_keys)}

    @app.put("/v1/master/autonomy/budget")
    def master_budget_update(body: MasterBudgetBody, request: Request) -> dict:
        user = console_user(request, "console.write", target="master", action="daily-budget-update")
        with ctx.db.transaction():
            ctx.db.run("INSERT INTO master_settings (singleton,daily_budget_minutes,updated_by) VALUES (true,%s,%s)"
                       " ON CONFLICT (singleton) DO UPDATE SET daily_budget_minutes=EXCLUDED.daily_budget_minutes,"
                       " updated_by=EXCLUDED.updated_by,updated_at=now()",
                       (body.daily_budget_minutes, user.subject))
            publish_for(ctx, MasterDailyBudgetUpdated(actor=user.subject,
                                                       daily_budget_minutes=body.daily_budget_minutes))
        return {"budget": master_budget_state()}

    @app.get("/v1/master/report")
    def master_report(request: Request, weeks: int = Query(8, ge=1, le=52)) -> dict:
        user = console_user(request, "console.read")
        return autonomy_report(weeks, scope=user.project_keys)

    @app.post("/v1/master/actions")
    def master_action(body: MasterActionBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, body.action,
                         {"ticket": body.ticket, "intent_chars": len(body.intent), "action": body.action})
        ticket = body.ticket.strip()
        ticket_row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket,)) if ticket else None
        policy_project = ticket_row["project"] if ticket_row else None
        state = MasterActionState(run, node, policy_project, ticket, body)
        policy = ctx.repo().policy_for(policy_project)
        require_intent(ctx, audit, state)
        decision = decide(ctx, state, policy)
        if decision.decision == "escalate":
            return escalate(ctx, audit, state, decision, log)
        if decision.decision != "allow":
            deny_policy_decision(ctx, audit, state, decision)
        ticket_row = validate_ticket_action(ctx, audit, state)
        result, ticket = perform_action(ctx, audit, state, ticket_row, process_flow_bugs, ticket)
        publish_for(ctx, MasterActionCompleted(run_id=body.run_id, fence=body.fence, ticket=ticket,
                                               actor=run["account_id"], action=body.action,
                                               result=str(result)[:500], intent=body.intent[:1000],
                                               why=body.why[:1000], account_id=body.account_id,
                                               target_run_id=body.target_run_id, signature=body.signature))
        master_action_event(ctx, state, "completed", body.why or body.intent or str(result))
        return {"ok": True, "result": result, "escalated": False}

    return SimpleNamespace(master_context=master_context, master_flow_bugs=master_flow_bugs,
                           master_plan_outcome=master_plan_outcome, autonomy_report=autonomy_report,
                           master_autonomy=master_autonomy, master_budget_update=master_budget_update,
                           master_report=master_report, master_action=master_action)
