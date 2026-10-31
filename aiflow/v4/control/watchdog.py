"""The control watchdog as a list of registered checks (JAIF-504).

Each check is one function registered in run order with `@check`; `run` calls them in that order and keeps one failing check from stopping
the others. Moved out of `loops.watchdog` without changing what any check does."""
from __future__ import annotations

import logging
from typing import Callable

from datetime import datetime, timezone
from . import environment as environment_reports
from . import merge_flow, role_health, tooling
from . import flow_doctor
from . import run_inbox as run_inbox_lifecycle
from . import system_check
from . import rollout
from ..core import work_limits
from ..core.services import service_gaps
from ..core.toolchains import toolchain_gaps
from .application_context import ApplicationContext
from .incidents import check_idle_flow, check_stack_pins, close_incident, open_incident
from .db import J
from .state import NODE_OFFLINE_S
from .quota import assessments as quota_assessments
from ..core.quota import as_utc

log = logging.getLogger("aiflow4.loops")  # the logger the watchdog used inside loops.py

CHECKS: list[tuple[str, Callable[[ApplicationContext], None]]] = []


def check(name: str):
    """Register a watchdog check; checks run in registration order."""
    def register(fn: Callable[[ApplicationContext], None]) -> Callable[[ApplicationContext], None]:
        CHECKS.append((name, fn))
        return fn
    return register


def run(ctx: ApplicationContext) -> None:
    for name, fn in CHECKS:
        try:
            fn(ctx)
        except Exception:  # noqa: BLE001 - a failing check must not stop the rest of the watchdog
            log.exception("watchdog check %s failed", name)


@check("node_heartbeats")
def node_heartbeats(ctx: ApplicationContext) -> None:
    now = datetime.now(timezone.utc)
    # nodes
    for n in ctx.db.all("SELECT name, status, last_heartbeat FROM nodes"):
        age = None if n["last_heartbeat"] is None else (now - n["last_heartbeat"]).total_seconds()
        if n["status"] == "online" and (age is None or age > NODE_OFFLINE_S):
            ctx.db.run("UPDATE nodes SET status='offline' WHERE name=%s", (n["name"],))
            ctx.events.emit("node.offline", node=n["name"], reason=f"no heartbeat for {int(age or 0)} s")
            open_incident(ctx, "node_down", n["name"], "critical", f"node {n['name']} is down",
                          f"No heartbeat for {int(age or 0)} seconds. Its runs lose their lease after 90 s and go back to To Do.")
            system_check.refresh_node(ctx, n["name"], "heartbeat_lost")
        elif n["status"] == "online":
            close_incident(ctx, "node_down", n["name"], "heartbeat is back")


@check("version_skew")
def version_skew(ctx: ApplicationContext) -> None:
    # version skew: a node outside the control/node policy gets no new work until it runs a compatible release
    from . import api as control_api
    rollout.watch_skew(ctx, control_api.RELEASES)


@check("node_tools")
def node_tools(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # project stacks: an online node must have every tool its projects need, or a person installs it
    repo = ctx.repo()
    tool_issues: set[str] = set()
    for n in ctx.db.all("SELECT name, status, tools, projects, dynamic FROM nodes"):
        for key in repo.projects_for_node(n["name"], n["projects"] or [], n["dynamic"]):
            project = repo.projects.get(key)
            if not project:
                continue
            subject = f"{n['name']}:{key}"
            if n["status"] != "online":
                # an offline node sends no heartbeat, so keep its incident: nobody confirmed the tools either way
                tool_issues.add(subject)
                continue
            gaps = toolchain_gaps(n["tools"], project.tools, repo.versions, project.stacks,
                                  require_pins=bool(project.stacks))
            if not gaps:
                continue
            tool_issues.add(subject)
            open_incident(ctx, "node_tools", subject, "warning", f"node {n['name']} misses tools for {key}",
                          f"Preflight on {n['name']} for {key} ({', '.join(project.stacks) or 'no stack'}) is unhealthy: "
                          f"{', '.join(gaps)}. The node agent installs stack tools and retries after a release or "
                          "toolchain update; the incident closes when a later heartbeat passes preflight.")
        # the CLIs of the AI providers whose accounts run on the node
        subject = f"{n['name']}:ai"
        ai_missing = toolchain_gaps(n["tools"], repo.provider_tools_for_node(n["name"]), repo.versions) \
            if n["status"] == "online" else []
        if n["status"] != "online" and repo.provider_tools_for_node(n["name"]):
            tool_issues.add(subject)
        elif ai_missing:
            tool_issues.add(subject)
            open_incident(ctx, "node_tools", subject, "warning", f"node {n['name']} misses AI tools",
                          f"Missing on {n['name']} for its accounts: {', '.join(ai_missing)}. Install them on the node; the"
                          " incident closes when a later heartbeat reports them.")
    for i in ctx.db.all("SELECT subject FROM incidents WHERE kind='node_tools' AND closed_at IS NULL"):
        if i["subject"] not in tool_issues:
            close_incident(ctx, "node_tools", i["subject"], "tools are installed or the node no longer serves the project")


@check("node_services")
def node_services(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # declared test services (PostgreSQL, Redis): an unhealthy one keeps the node from taking that project's work
    service_issues: set[str] = set()
    for n in ctx.db.all("SELECT name, status, metrics, projects, dynamic FROM nodes WHERE status='online'"):
        for key in repo.projects_for_node(n["name"], n["projects"] or [], n["dynamic"]):
            project = repo.projects.get(key)
            gaps = service_gaps((n["metrics"] or {}).get("services"), project.required_services) if project else {}
            if not gaps:
                continue
            subject = f"{n['name']}:{key}"
            service_issues.add(subject)
            open_incident(ctx, "node_services", subject, "warning", f"node {n['name']} has unhealthy test services for {key}",
                          f"{n['name']} takes no {key} work until its test services are healthy: "
                          + "; ".join(f"{svc} ({why})" for svc, why in gaps.items())
                          + ". The node agent recreates missing or stopped services; the incident closes when a later"
                          " heartbeat reports them healthy.")
    for i in ctx.db.all("SELECT subject FROM incidents WHERE kind='node_services' AND closed_at IS NULL"):
        if i["subject"] not in service_issues:
            close_incident(ctx, "node_services", i["subject"], "test services are healthy or the node no longer serves the project")


@check("tool_and_environment_waits")
def tool_and_environment_waits(ctx: ApplicationContext) -> None:
    # tickets waiting for a tool no machine has: the 24 hour clock and the one maintenance ticket per tool
    tooling.declare_undeclared_services(ctx)
    tooling.sweep(ctx)
    tooling.check_stalled_waits(ctx)
    # environment items a run reported missing, and the daily scan of open tickets for server wording
    environment_reports.sweep(ctx)
    environment_reports.scan_if_due(ctx)


@check("nodes_without_accounts")
def nodes_without_accounts(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # nodes that joined but have no AI account: they add slots, never work
    with_accounts = {m for a in repo.accounts for m in a.machines}
    for n in ctx.db.all("SELECT name, status FROM nodes"):
        if n["status"] == "online" and n["name"] not in with_accounts:
            where = "" if n["name"] in repo.nodes or repo.inventory_in_db else " and is not described in aiflow-control/nodes/"
            open_incident(ctx, "node_no_ai", n["name"], "warning", f"node {n['name']} has no AI account",
                          f"{n['name']} is online{where}, but no AI account is assigned to it, so it cannot take any ticket."
                          " Add and sign in to an AI account for this machine on the Machines and AI screen.")
        elif n["name"] in with_accounts or n["status"] != "online":
            close_incident(ctx, "node_no_ai", n["name"], "accounts assigned" if n["name"] in with_accounts else "node left")


@check("orphaned_tickets")
def orphaned_tickets(ctx: ApplicationContext) -> None:
    # orphans: Jira says In Progress but no run is working on it (a worker died between writes, a deploy, a manual
    # move). After 10 quiet minutes the ticket goes back to To Do so it is never abandoned.
    if ctx.jira:
        for t in ctx.db.all(
                "SELECT t.key FROM tickets t WHERE t.status = 'In Progress'"
                " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = t.key AND r.status = 'running')"
                " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = t.key AND r.started_at > now() - interval '10 minutes')"
                " AND t.synced_at > now() - interval '5 minutes'"):
            if ctx.leases.holder(t["key"]):
                continue
            try:
                if ctx.jira.status(t["key"]) != "In Progress":
                    continue
                ctx.jira.transition_to(t["key"], "To Do")
                ctx.jira.comment(t["key"], "AI Flow · watchdog · requeued\nWhat: The ticket was In Progress but no run had worked on it"
                                 " for 10 minutes.\nWhy: A worker stopped between two updates, so nobody owned the ticket.\n"
                                 "Next: Back to To Do; the next worker continues its branch.")
                ctx.db.run("UPDATE tickets SET status='To Do' WHERE key=%s", (t["key"],))
                merge_flow.record_move(ctx, t["key"], "In Progress", "To Do", merge_flow.FLOW,
                                       "watchdog: In Progress without a run for 10 minutes")
                ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result) VALUES"
                           " ('jira','transition',%s,'control',true,'To Do (orphan)')", (t["key"],))
                ctx.events.emit("jira.transition", ticket=t["key"], account="control", outcome="To Do",
                                reason="In Progress without a run for 10 minutes")
                ctx.events.emit("ticket.requeued", ticket=t["key"], reason="In Progress without a run for 10 minutes")
            except Exception as e:  # noqa: BLE001
                log.warning("could not requeue orphan %s: %s", t["key"], e)


@check("stuck_tickets")
def stuck_tickets(ctx: ApplicationContext) -> None:
    # tickets whose dev runs keep failing (backoff in the API keeps them from spinning; a person should look)
    from .features.work.constants import BACKOFF_SQL
    streak_sql = BACKOFF_SQL.split("SELECT ticket_key, fails FROM streak")[0] + "SELECT ticket_key, fails FROM streak"
    stuck: dict[str, int] = {}
    for step in ("dev", "verify"):
        for r in ctx.db.all(streak_sql, {"step": step}):
            stuck[r["ticket_key"]] = max(stuck.get(r["ticket_key"], 0), r["fails"])
    for key, fails in stuck.items():
        if fails >= 3:
            last = ctx.db.one("SELECT account_id, summary FROM runs WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 1", (key,))
            open_incident(ctx, "ticket_stuck", key, "warning", f"{key} failed {fails} runs in a row",
                          f"Last error ({last['account_id'] if last else '?'}): {(last['summary'] if last else '')[:300]}."
                          " Retries wait up to 30 minutes; fix the cause or put the ticket on hold.")
    for i in ctx.db.all("SELECT subject FROM incidents WHERE kind='ticket_stuck' AND closed_at IS NULL"):
        if i["subject"].startswith("tool_wait:"):
            continue  # tooling.check_stalled_waits owns the two-hour missing-requirement warning lifecycle
        if stuck.get(i["subject"], 0) < 3:
            close_incident(ctx, "ticket_stuck", i["subject"], "a run produced work again")


@check("retry_and_deadline_passes")
def retry_and_deadline_passes(ctx: ApplicationContext) -> None:
    if ctx.jira:  # the third unsuccessful run in a row ends the automatic retries of that ticket
        from . import retry_hold
        try:
            retry_hold.apply(ctx)
        except Exception as e:  # noqa: BLE001 - a failing hold pass must not stop the rest of the watchdog
            log.warning("retry hold pass failed: %s", e)

    try:  # an unanswered owner question with a reversible recommendation is applied after its deadline
        from . import escalation_deadline
        escalation_deadline.apply_due(ctx)
    except Exception as e:  # noqa: BLE001 - a failing deadline pass must not stop the rest of the watchdog
        log.warning("escalation deadline pass failed: %s", e)
    # a flow that stands still and a configuration that keeps a project from getting work
    try:
        check_stack_pins(ctx)
        check_idle_flow(ctx)
    except Exception as e:  # noqa: BLE001 - a failing check must not stop the rest of the watchdog
        log.warning("idle and pin checks failed: %s", e)


@check("lost_leases_and_run_clocks")
def lost_leases_and_run_clocks(ctx: ApplicationContext) -> None:
    now = datetime.now(timezone.utc)
    # runs whose lease disappeared (worker died, node down, network split)
    for r in ctx.db.all("SELECT * FROM runs WHERE status='running'"):
        holder = ctx.leases.holder(r["ticket_key"])
        if holder != f"{r['id']}:{r['fence']}":
            lost, _undelivered = run_inbox_lifecycle.watchdog_lost(ctx, r)
            if not lost:
                continue
            ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'"
                       " AND NOT EXISTS (SELECT 1 FROM runs WHERE account_id=%s AND status='running' AND id<>%s)",
                       (r["account_id"], r["account_id"], r["id"]))
            ctx.events.emit("run.lost", ticket=r["ticket_key"], run_id=str(r["id"]), fence=r["fence"], account=r["account_id"],
                            node=r["node"], reason="lease expired")
            if ctx.jira and not holder:
                try:
                    st = ctx.jira.status(r["ticket_key"])
                    if st == "In Progress":
                        ctx.jira.transition_to(r["ticket_key"], "To Do")
                        ctx.jira.comment(r["ticket_key"], f"AI Flow: the run on {r['node']} ({r['account_id']}) stopped"
                                         " sending heartbeats; the ticket is back in To Do and keeps its branch for the next run.")
                        ctx.db.run("INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result)"
                                   " VALUES ('jira','transition',%s,%s,%s,'control',true,'To Do (lease lost)')",
                                   (r["ticket_key"], str(r["id"]), r["fence"]))
                        ctx.db.run("UPDATE tickets SET status='To Do' WHERE key=%s", (r["ticket_key"],))
                        merge_flow.record_move(ctx, r["ticket_key"], "In Progress", "To Do", merge_flow.FLOW,
                                               f"watchdog: the run on {r['node']} lost its lease")
                        ctx.events.emit("jira.transition", ticket=r["ticket_key"], run_id=str(r["id"]), fence=r["fence"],
                                        account="control", node=r["node"], outcome="To Do", reason="lease lost")
                except Exception as e:  # noqa: BLE001
                    log.warning("could not requeue %s: %s", r["ticket_key"], e)
            continue
        # clocks: 30 target, 45 warn, 60 hard (per project)
        p = ctx.repo().projects.get(r["project"])
        wm = p.work_minutes() if p else dict(work_limits.DEFAULTS)
        minutes = (now - r["started_at"]).total_seconds() / 60
        if minutes >= wm["hard"]:
            open_incident(ctx, "run_hard_limit", r["ticket_key"], "critical", f"{r['ticket_key']} passed {wm['hard']} minutes",
                          f"Run {r['id']} on {r['node']} ({r['account_id']}) has worked {int(minutes)} minutes.")
        elif minutes >= wm["warn"]:
            open_incident(ctx, "run_slow", r["ticket_key"], "warning", f"{r['ticket_key']} passed {wm['warn']} minutes",
                          f"Run {r['id']} on {r['node']} ({r['account_id']}) has worked {int(minutes)} minutes.")
    for kind in ("run_slow", "run_hard_limit"):
        for i in ctx.db.all("SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (kind,)):
            if not ctx.db.one("SELECT 1 FROM runs WHERE ticket_key=%s AND status='running'", (i["subject"],)):
                close_incident(ctx, kind, i["subject"], "run finished")


@check("account_cooldowns")
def account_cooldowns(ctx: ApplicationContext) -> None:
    # accounts
    for a in ctx.db.all("SELECT id, status, cooldown_until FROM accounts WHERE status='cooldown'"):
        if not ctx.leases.cooling(a["id"]):
            ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s", (a["id"],))
            close_incident(ctx, "account_quota", a["id"], "cooldown ended")
    for a in ctx.db.all("SELECT id, provider, status, cooldown_until, last_error FROM accounts"
                        " WHERE status IN ('cooldown','signed_out','error')"):
        what = {"cooldown": "is out of quota", "signed_out": "is signed out", "error": "fails its check"}[a["status"]]
        until = f" until {a['cooldown_until']:%Y-%m-%d %H:%M} UTC" if a["cooldown_until"] else ""
        open_incident(ctx, "account_quota", a["id"], "warning", f"account {a['id']} {what}",
                      f"{a['provider']}: {a['last_error'][:300]}{until}. Its tickets go to the other accounts.")
    for a in ctx.db.all("SELECT id FROM accounts WHERE status IN ('idle','working')"):
        close_incident(ctx, "account_quota", a["id"], "account works again")


@check("role_coverage")
def role_coverage(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # every role the flow needs must have at least two ready accounts (no single AI the flow depends on)
    online = {r["name"] for r in ctx.db.all("SELECT name FROM nodes WHERE status = 'online'")}
    by_id = {a.id: a for a in repo.accounts}
    ready = {r["id"] for r in ctx.db.all("SELECT id FROM accounts WHERE status IN ('idle','working','probing')")
             if r["id"] in by_id and online.intersection(by_id[r["id"]].machines)}
    try:  # quality by role: switch roles off and back on, settle probation tickets (role_health.py)
        role_health.evaluate(ctx)
    except Exception as e:  # noqa: BLE001
        log.warning("role health evaluation failed: %s", e)
    switched_off = role_health.disabled_roles(ctx.db)
    for role in flow_doctor.ROLE_COVERAGE_ROLES:
        holders = [a for a in repo.accounts if role in (a.roles or [])]
        ok = [a for a in holders if a.id in ready and not a.paused and (a.id, role) not in switched_off]
        if not holders:
            continue
        providers = {a.provider for a in ok}
        if len(providers) < 2:  # two ready accounts of the same provider do not survive that provider's quota or outage
            open_incident(ctx, "role_coverage", role, "critical" if not ok else "warning",
                          f"role {role} has {len(providers)} ready provider{'s' if len(providers) != 1 else ''}",
                          f"Accounts with the role: {', '.join(a.id for a in holders)}; ready: "
                          f"{', '.join(a.id for a in ok) or 'none'} across "
                          f"{', '.join(sorted(providers)) or 'no providers'}. Every role needs two ready accounts of "
                          "different providers so one quota or outage cannot stop the flow.")
        else:
            close_incident(ctx, "role_coverage", role, f"{len(ok)} accounts from {len(providers)} providers ready")


@check("quota_windows")
def quota_windows(ctx: ApplicationContext) -> None:
    # quota windows reported by the provider (Claude: five_hour and seven_day utilization with reset times)
    for a in ctx.db.all("SELECT id,provider,quota FROM accounts WHERE quota_source<>'' AND quota ? 'windows'"):
        for wname, w in (a["quota"].get("windows") or {}).items():
            u = w.get("utilization")
            if u is None:
                continue
            u = float(u)
            reset = w.get("resets_at")
            reset_at = as_utc(reset)
            reset_txt = reset_at.strftime("%Y-%m-%d %H:%M UTC") if reset_at else "unknown"
            label = {"five_hour": "5-hour", "seven_day": "weekly"}.get(wname, wname)
            subject = f"{a['id']}:{wname}"
            if u >= 0.9:
                sev = "critical" if u >= 1.0 else "warning"
                tip = (" If the account has a limit reset offer, Settings > Usage > Reset for free restores it now."
                       if a["provider"] == "claude" and wname == "seven_day" else "")
                open_incident(ctx, "quota_high", subject, sev, f"{a['id']} {label} limit at {round(u * 100)}%",
                              f"Resets at {reset_txt}. At 100% the account pauses until then and other accounts take"
                              f" its tickets.{tip}")
            elif u < 0.85:
                close_incident(ctx, "quota_high", subject, f"{label} usage back to {round(u * 100)}%")


@check("quota_forecasts")
def quota_forecasts(ctx: ApplicationContext) -> None:
    account_rows = ctx.db.all("SELECT id,provider,quota,quota_source,quota_observed_at FROM accounts ORDER BY id")
    account_providers = {str(row["id"]): str(row["provider"]) for row in account_rows}
    for account_id, awareness in quota_assessments(ctx.db, account_rows).items():
        at_risk = [w for w in awareness["windows"]
                   if w["prediction"]["status"] == "will run out before reset"]
        ctx.db.run("UPDATE accounts SET quota_at_risk=%s,quota_forecast=%s WHERE id=%s",
                   (bool(at_risk), J(awareness), account_id))
        if at_risk:
            soonest = min((w["prediction"].get("exhaustion_at") or "") for w in at_risk)
            windows = ", ".join(w["label"] for w in at_risk)
            open_incident(ctx, "quota_prediction", account_id, "warning",
                          f"{account_id} may run out before reset",
                          f"Predicted exhaustion: {soonest or 'before reset'} UTC ({windows}). "
                          f"Confidence: {awareness['confidence']}. New work is routed to other "
                          f"{account_providers.get(account_id, 'AI')} accounts first.")
        else:
            close_incident(ctx, "quota_prediction", account_id, "quota forecast is no longer before reset")
