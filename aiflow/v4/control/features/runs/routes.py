"""Routes of the runs feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from .bodies import FenceBody, MasterActionBody, MasterBudgetBody, MasterFlowBugsBody, MasterPlanOutcomeBody, ResultBody
from typing import Any
from fastapi import Depends, HTTPException, Query, Request
from ....core import work_limits
from ... import (escalations,
               knowledge, load_policy, quota_refresh, role_health, support_bundle, tooling)
from ... import run_inbox as run_inbox_control
from ... import environment as environment_reports
from ... import quota as quota_reporting
from ...incidents import flow_bug_candidates
from ...db import J
from ...policy import decide_master_action
from ... import notify
from ...features.work.constants import utcnow
from ...features.work.listeners import run_finished


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module
    audit = deps.audit
    console_user = deps.console_user
    last_dev = deps.last_dev
    mark_machine_auth_failure = deps.mark_machine_auth_failure
    master_budget_state = deps.master_budget_state
    master_run = deps.master_run
    master_snapshot = deps.master_snapshot
    node_auth = deps.node_auth
    process_flow_bugs = deps.process_flow_bugs
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
        late_result = run_inbox_control.finish(ctx, run, run_id, body, details, node)
        account_update_allowed = not late_result and still_owner
        status_update_allowed = account_update_allowed and run_inbox_control.account_update_allowed(
            ctx, run_id, run["account_id"], still_owner)
        if late_result:
            outcome = "lost"
        elif isinstance(run_log, str) and run_log:
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
        run_finished(ctx, run_id=run_id, ticket_key=run["ticket_key"], project=run["project"], step=run["step"],
                     fence=body.fence, account=run["account_id"], provider=run["provider"], model=run["model"], node=node,
                     duration_ms=dur, outcome=outcome, reason=error_kind or body.summary[:200], usage_in=body.tokens_in,
                     usage_out=body.tokens_out, cost_usd=body.cost_usd, still_owner=still_owner, pr_url=body.pr_url)
        try:  # quality by role: this result may switch the role off or settle a probation ticket
            if not late_result:
                role_health.evaluate(ctx, run["account_id"])
                if run["step"] == "verify":  # a verdict also decides the dev run it judged
                    dev = last_dev(run["ticket_key"])
                    if dev and dev["account_id"] != run["account_id"]:
                        role_health.evaluate(ctx, dev["account_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("role health after run %s failed: %s", run_id, e)
        return {"ok": True, "still_owner": still_owner}

    @app.post("/v1/runs/{run_id}/release")
    def run_release(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        released = run_inbox_control.release(ctx, run, run_id, body, node)
        ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'", (run["account_id"],))
        ctx.events.emit("run.released", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="released" if released else "not_owner")
        return {"released": released}

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
        inserted = ctx.db.one("INSERT INTO master_plan_cycles (run_id, valid) VALUES (%s,%s)"
                              " ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
                              (body.run_id, body.valid))
        recorded = (ctx.db.one("SELECT valid FROM master_plan_cycles WHERE run_id=%s", (body.run_id,))
                    if not inserted else {"valid": body.valid})
        valid = bool(recorded["valid"])
        if inserted:
            ctx.events.emit("master.plan", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                            account=run["account_id"], node=node, outcome="valid" if valid else "invalid",
                            reason="strict-format retry completed" if not valid else "valid action plan")
        streak_row = ctx.db.one(
            "SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid AND recorded_at > "
            "coalesce((SELECT max(recorded_at) FROM master_plan_cycles WHERE valid), 'epoch'::timestamptz)") or {}
        hour_row = ctx.db.one("SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid"
                              " AND recorded_at >= now()-interval '1 hour'") or {}
        streak, last_hour = int(streak_row.get("n") or 0), int(hour_row.get("n") or 0)
        incident_open = False
        if valid:
            from ...incidents import close_incident
            close_incident(ctx, "master_invalid_plan", "global", "master returned a valid action plan")
        elif streak >= 3 or last_hour >= 5:
            from ...incidents import open_incident
            incident_open = open_incident(
                ctx, "master_invalid_plan", "global", "critical", "master repeatedly returned an invalid plan",
                f"Invalid plan cycles: {streak} in a row and {last_hour} in the last hour. "
                "The master retried once with a strict JSON reminder on each cycle.")
        return {"valid": valid, "invalid_streak": streak, "invalid_last_hour": last_hour,
                "incident_opened": incident_open}

    def autonomy_report(weeks: int = 8, scope: tuple[str, ...] | None = None) -> dict[str, Any]:
        now = utcnow()
        this_monday = now.date() - timedelta(days=now.weekday())
        first_monday = this_monday - timedelta(days=(weeks - 1) * 7)
        start = datetime.combine(first_monday, datetime.min.time(), tzinfo=timezone.utc)
        end = now
        trend = []
        for index in range(weeks):
            week = first_monday + timedelta(days=index * 7)
            trend.append({"week_start": week.isoformat(), "finished_without_person": 0,
                          "needed_person": 0, "repeated_failures": 0})
        def trend_index(value: datetime) -> int | None:
            date = value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
            offset = (date - first_monday).days // 7
            return offset if 0 <= offset < weeks else None

        # A person limited to some projects gets the report of those projects only.
        ticket_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))" if scope is not None else ""
        scope_params: tuple = (list(scope),) if scope is not None else ()
        finished = ctx.db.all(
            "SELECT DISTINCT t.key, t.summary, t.done_at FROM tickets t WHERE t.status='Done'"
            + (" AND t.project=ANY(%s)" if scope is not None else "") +
            " AND t.done_at >= %s AND t.done_at <= %s AND EXISTS (SELECT 1 FROM runs r"
            " WHERE r.ticket_key=t.key AND r.status<>'running' AND lower(r.outcome) IN ('done','pass','passed')"
            " AND r.started_at <= t.done_at AND NOT EXISTS (SELECT 1 FROM runs later"
            " WHERE later.ticket_key=t.key AND later.started_at > r.started_at AND later.started_at <= t.done_at))"
            " AND NOT EXISTS (SELECT 1 FROM write_audit w WHERE w.ticket=t.key AND w.action='transition'"
            " AND w.payload->>'source'='human-review' AND w.result='Done' AND w.ts <= t.done_at)", scope_params + (start, end))
        for item in finished:
            i = trend_index(item["done_at"])
            if i is not None:
                trend[i]["finished_without_person"] += 1
        needed = ctx.db.all("SELECT observation_key, ticket, reason, options, sent_at FROM master_escalations"
                            " WHERE sent_at >= %s AND sent_at <= %s" + ticket_filter + " ORDER BY sent_at DESC",
                            (start, end) + scope_params)
        for item in needed:
            i = trend_index(item["sent_at"])
            if i is not None:
                trend[i]["needed_person"] += 1
        repeated = ctx.db.all("SELECT ts, type FROM events WHERE type IN ('flow_bug.created','flow_bug.repeated')"
                              " AND ts >= %s AND ts <= %s" + ticket_filter, (start, end) + scope_params)
        for item in repeated:
            i = trend_index(item["ts"])
            if i is not None:
                trend[i]["repeated_failures"] += 1
        week_start = datetime.combine(this_monday, datetime.min.time(), tzinfo=timezone.utc)
        current_finished = [row for row in finished if row["done_at"] >= week_start]
        current_needed = [row for row in needed if row["sent_at"] >= week_start]
        current_repeated = ctx.db.all(
            "SELECT f.ticket_key, f.signature, f.repeat_count, f.updated_at, t.summary, t.status"
            " FROM flow_bug_tracker f LEFT JOIN tickets t ON t.key=f.ticket_key"
            " WHERE f.updated_at >= %s" + (" AND t.project=ANY(%s)" if scope is not None else "") +
            " ORDER BY f.updated_at DESC LIMIT 50", (week_start,) + scope_params)
        # Accounts are shared infrastructure, not project data.
        quota_accounts = ctx.db.all("SELECT id,provider,quota,quota_source,quota_observed_at FROM accounts ORDER BY id") \
            if scope is None else []
        quota_state = quota_reporting.assessments(ctx.db, quota_accounts, now)
        return {"week_start": this_monday.isoformat(),
                "finished_without_person": current_finished[:50], "needed_person": current_needed[:50],
                "repeated_failures": current_repeated, "trend": trend,
                "escalations": escalations.weekly_metrics(ctx.db, weeks),
                "quota_accounts": [{"id": a["id"], "provider": a["provider"],
                                    "quota_awareness": quota_state.get(a["id"], {})} for a in quota_accounts]}

    @app.get("/v1/master/autonomy")
    def master_autonomy(request: Request) -> dict:
        user = console_user(request, "console.read")
        return {"budget": master_budget_state(), "report": autonomy_report(scope=user.project_keys)}

    @app.put("/v1/master/autonomy/budget")
    def master_budget_update(body: MasterBudgetBody, request: Request) -> dict:
        user = console_user(request, "console.write", target="master", action="daily-budget-update")
        ctx.db.run("INSERT INTO master_settings (singleton,daily_budget_minutes,updated_by) VALUES (true,%s,%s)"
                   " ON CONFLICT (singleton) DO UPDATE SET daily_budget_minutes=EXCLUDED.daily_budget_minutes,"
                   " updated_by=EXCLUDED.updated_by,updated_at=now()",
                   (body.daily_budget_minutes, user.subject))
        audit("master", "daily-budget-update", "", "", None, user.subject, True,
              f"daily budget set to {body.daily_budget_minutes} minutes",
              {"daily_budget_minutes": body.daily_budget_minutes})
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
        policy = ctx.repo().policy_for(policy_project)
        if not body.intent.strip():
            reason = "master action must include the request or observation intent"
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], run_id=body.run_id,
                            fence=body.fence, account=run["account_id"], node=node, outcome="denied", reason=reason,
                            action=body.action)
            raise HTTPException(400, reason)
        # Only the request is matched: what/why/result/detail describe observations (an offline node, a run
        # past its hard limit) and must not turn an allowed follow-up into an owner escalation.
        decision = decide_master_action(policy, body.action, body.intent, body.category)
        ctx.events.emit("policy.decision", ticket=ticket or run["ticket_key"], step="master", run_id=body.run_id,
                        fence=body.fence, account=run["account_id"], node=node, outcome=decision.decision,
                        reason=decision.reason[:200], action=body.action)

        def master_event(outcome: str, reason: str) -> None:
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], project=policy_project or "",
                            step="master", run_id=body.run_id, fence=body.fence, account=run["account_id"],
                            provider=run["provider"], model=run["model"], node=node, outcome=outcome,
                            reason=reason[:300], action=body.action)

        def refuse_master_action(reason: str, status: int = 409) -> None:
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason,
                  {"intent": body.intent[:1000], "why": body.why[:1000]})
            master_event("denied", reason)
            raise HTTPException(status, reason)

        if decision.decision == "escalate":
            options = [str(option).strip()[:500] for option in body.options if str(option).strip()][:5]
            if body.action == "escalate_to_owner" and (not body.why.strip() or not options):
                refuse_master_action("escalate_to_owner requires a reason and available options", 400)
            if not options:
                options = ["Keep the current state and wait for the next health check.",
                           "Choose another healthy account or retry after the reported reset time.",
                           "Ask an owner to investigate and decide the next step."]
            why = (body.why or f"The policy requires a person to decide: {decision.reason}").strip()[:1500]
            observation_key = body.observation_key.strip()[:500]
            if not observation_key:
                if body.kind and body.subject:
                    observation_key = f"incident:{body.kind}:{body.subject}"
                elif ticket:
                    updated = ctx.db.one("SELECT jira_updated FROM tickets WHERE key=%s", (ticket,))
                    observation_key = f"ticket:{ticket}:{updated['jira_updated'].isoformat() if updated and updated['jira_updated'] else 'current'}"
                else:
                    observation_key = f"master:{body.action}:{body.intent[:200]}"
            configured_channels = notify.configured_channels(ctx, "ticket_needs_person")
            created_row = ctx.db.one(
                "INSERT INTO master_escalations"
                " (observation_key,ticket,reason,options,run_id,pending_channels,delivered_channels,intent)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (observation_key) DO NOTHING RETURNING observation_key",
                (observation_key, ticket, why, J(options), body.run_id, configured_channels, [], body.intent[:1500]))
            escalation = ctx.db.one(
                "SELECT reason, options, pending_channels, delivered_channels, intent FROM master_escalations"
                " WHERE observation_key=%s", (observation_key,))
            # A NULL pending list marks rows created before channel delivery tracking was installed. Retry all
            # currently configured channels for those rows; the prior version could not record partial failures.
            pending_channels = set(escalation.get("pending_channels") or [])
            delivered_channels = set(escalation.get("delivered_channels") or [])
            if escalation.get("pending_channels") is None:
                pending_channels.update(set(configured_channels) - delivered_channels)
            elif not pending_channels and not delivered_channels:
                # An observation first seen with no alert channels can be delivered if channels are configured
                # later. Successfully delivered observations have a non-empty delivered list and stay suppressed.
                pending_channels.update(configured_channels)
            else:
                pending_channels.update(set(configured_channels) - delivered_channels)
            pending_before = sorted(pending_channels)
            if pending_before != sorted(escalation.get("pending_channels") or []):
                ctx.db.run("UPDATE master_escalations SET pending_channels=%s WHERE observation_key=%s",
                           (pending_before, observation_key))
            link = (ctx.settings.jira_site.rstrip("/") + f"/browse/{ticket}") if ticket and ctx.settings.jira_site else ""
            why = str(escalation["reason"])
            stored_options = [str(option) for option in (escalation.get("options") or [])]
            message = (f"Why a person is needed: {why}\nRequest: {escalation.get('intent') or body.intent[:1500]}\n"
                       "Available options:\n" + "\n".join(f"- {option}" for option in stored_options))
            attempt_channels = sorted(pending_channels.intersection(configured_channels))
            try:
                sent = (notify.send("warning", f"{ticket or 'AI Flow'} requires an owner decision", message, link,
                                    ctx=ctx, alert_kind="ticket_needs_person", project=policy_project or "AI Flow",
                                    ticket=ticket, needed=why, channels=attempt_channels, queued_ok=True)
                        if attempt_channels else [])
            except Exception as exc:  # Persist the pending channels and retry them on a later master cycle.
                log.warning("master escalation notification failed: %s", type(exc).__name__)
                sent = []
            sent = sorted(set(sent).intersection(attempt_channels))
            pending_channels.difference_update(sent)
            delivered_channels.update(sent)
            ctx.db.run("UPDATE master_escalations SET pending_channels=%s, delivered_channels=%s"
                       " WHERE observation_key=%s",
                       (sorted(pending_channels), sorted(delivered_channels), observation_key))
            already_sent = bool(not created_row and delivered_channels and not pending_channels and not sent)
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"],
                  body.action == "escalate_to_owner", decision.reason,
                  {"intent_chars": len(body.intent), "observation_key": observation_key, "options": options})
            audit("notify", "master-escalation", ticket, body.run_id, body.fence, run["account_id"], True,
                  ",".join(sent) if sent else ("pending: " + ",".join(sorted(pending_channels))
                                                if pending_channels else "no channel configured or duplicate"),
                  {"reason": why, "options": stored_options, "observation_key": observation_key,
                   "pending_channels": sorted(pending_channels)})
            master_event("escalated", why)
            return {"ok": True, "escalated": True, "reason": why, "options": stored_options,
                    "already_sent": already_sent, "sent": sent, "pending_channels": sorted(pending_channels)}

        if decision.decision != "allow":
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False,
                  decision.reason, {"intent_chars": len(body.intent)})
            master_event("denied", decision.reason)
            raise HTTPException(403, decision.reason)

        allowed_ticket_actions = {"retry", "move_to_todo", "restart_run"}
        ticket_row = None
        if body.action in allowed_ticket_actions:
            ticket_row = ctx.db.one("SELECT key, project, status FROM tickets WHERE key=%s", (ticket,)) if ticket else None
            if not ticket_row:
                reason = "a valid Jira ticket is required for this master action"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
                master_event("denied", reason)
                raise HTTPException(400, reason)
            if not ctx.jira:
                refuse_master_action("Jira is unavailable", 503)

        result = "ok"
        if body.action in ("retry", "move_to_todo"):
            if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
                refuse_master_action(f"a ticket in {ticket_row['status']} cannot be retried by the master")
            if body.action == "move_to_todo" and ticket_row["status"] not in ("To Do", "In Progress", "In Verify"):
                refuse_master_action("move_to_todo is limited to tickets in To Do, In Progress or In Verify")
            if ctx.leases.holder(ticket):
                refuse_master_action("ticket still has an active run; use restart_run after its work-time limit")
            if body.action == "retry":
                latest = ctx.db.one("SELECT outcome FROM runs WHERE ticket_key=%s AND status<>'running'"
                                    " ORDER BY started_at DESC LIMIT 1", (ticket,))
                if not latest or latest["outcome"] not in ("failed", "timeout", "lost", "quota", "blocked", "no_changes"):
                    refuse_master_action("retry requires a recent failed, blocked, quota or lost run")
            try:
                status = ctx.jira.transition_to(ticket, "To Do")
            except Exception as exc:  # noqa: BLE001 - record the refusal without exposing Jira internals
                refuse_master_action(f"Jira could not move {ticket} to To Do ({type(exc).__name__})", 502)
            ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, ticket))
            result = f"ticket moved to {status}"
        elif body.action == "pause_account":
            account_id = body.account_id.strip()
            account = ctx.db.one("SELECT id, status FROM accounts WHERE id=%s", (account_id,)) if account_id else None
            if not account:
                refuse_master_action("pause_account requires a configured account_id", 400)
            unhealthy = account["status"] in ("cooldown", "signed_out", "error") or bool(
                ctx.db.one("SELECT 1 AS issue FROM incidents WHERE closed_at IS NULL"
                           " AND kind IN ('account_quota','quota_high') AND (subject=%s OR subject LIKE %s)",
                           (account_id, account_id + ":%")))
            if not unhealthy:
                refuse_master_action("master may pause only an account with a quota or health issue")
            minutes = min(1440, max(1, int(body.pause_minutes)))
            ctx.db.run("INSERT INTO master_account_pauses (account_id,paused_until,reason,run_id)"
                       " VALUES (%s,now()+(%s * interval '1 minute'),%s,%s) ON CONFLICT (account_id) DO UPDATE SET"
                       " paused_until=EXCLUDED.paused_until,reason=EXCLUDED.reason,run_id=EXCLUDED.run_id,updated_at=now()",
                       (account_id, minutes, (body.why or body.intent)[:500], body.run_id))
            result = f"account paused for {minutes} minutes"
            ticket = ""
        elif body.action == "restart_run":
            if not body.target_run_id or not ctx.jira:
                refuse_master_action("restart_run requires target_run_id and an available Jira connection", 400)
            target = ctx.db.one("SELECT * FROM runs WHERE id::text=%s", (body.target_run_id,))
            if not target:
                refuse_master_action("target_run_id does not identify a recorded run", 404)
            if (target["ticket_key"] != ticket or target["step"] == "master" or target["status"] != "running"
                    or not ctx.leases.valid(target["ticket_key"], str(target["id"]), int(target["fence"]))):
                refuse_master_action("target run is not the active fenced run for this ticket")
            if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
                refuse_master_action(f"a run on a ticket in {ticket_row['status']} cannot be restarted by the master")
            project = ctx.repo().projects.get(target["project"])
            hard = project.work_minutes()["hard"] if project else work_limits.HARD
            elapsed = max(0.0, (utcnow() - target["started_at"]).total_seconds() / 60)
            if elapsed < hard:
                refuse_master_action(f"target run has not passed its {hard} minute work-time limit")
            if not ctx.leases.release(target["ticket_key"], str(target["id"]), int(target["fence"])):
                refuse_master_action("target run lease changed before restart")
            run_inbox_control.restart(ctx, target, body.target_run_id, body.run_id, body.why or body.intent)
            ctx.db.run("UPDATE accounts SET status='idle',updated_at=now() WHERE id=%s AND status='working'",
                       (target["account_id"],))
            try:
                status = ctx.jira.transition_to(ticket, "To Do")
            except Exception as exc:  # noqa: BLE001 - the old lease is already fenced off
                reason = f"run {body.target_run_id} was stopped, but Jira could not move {ticket} to To Do ({type(exc).__name__})"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], True, reason,
                      {"intent": body.intent[:1000], "why": body.why[:1000], "target_run_id": body.target_run_id})
                master_event("partial", reason)
                raise HTTPException(502, reason) from exc
            ctx.db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, ticket))
            result = f"stuck run {body.target_run_id} stopped; ticket moved to {status}"
            ticket_row["project"] = target["project"]
        elif body.action == "open_bug":
            candidates = {candidate["signature"] for candidate in flow_bug_candidates(ctx)
                          if candidate["creation_eligible"]}
            if not body.signature or body.signature not in candidates:
                refuse_master_action("open_bug requires an eligible repeated flow failure signature")
            reports = process_flow_bugs(run)
            matching = next((report for report in reports if report.get("signature") == body.signature), None)
            if not matching:
                tracked = ctx.db.one("SELECT ticket_key FROM flow_bug_tracker WHERE signature=%s", (body.signature,))
                matching = {"action": "already_reported", "ticket": tracked["ticket_key"]} if tracked else None
            if not matching:
                refuse_master_action("the eligible flow failure could not be recorded; it will be retried next cycle", 502)
            ticket = matching.get("ticket", "")
            result = f"{matching['action']} {ticket}".strip()
        audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], True, str(result)[:500],
              {"intent": body.intent[:1000], "why": body.why[:1000], "account_id": body.account_id,
               "target_run_id": body.target_run_id, "signature": body.signature})
        master_event("completed", body.why or body.intent or str(result))
        return {"ok": True, "result": result, "escalated": False}

    return SimpleNamespace(run_heartbeat=run_heartbeat, run_result=run_result, run_release=run_release, master_context=master_context, master_flow_bugs=master_flow_bugs, master_plan_outcome=master_plan_outcome, autonomy_report=autonomy_report, master_autonomy=master_autonomy, master_budget_update=master_budget_update, master_report=master_report, master_action=master_action)
