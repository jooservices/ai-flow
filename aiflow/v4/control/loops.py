"""Background loops of the control plane (process `aiflow4 control-loops`).

jira_sync   every 20 s: tickets updated in the last 5 minutes; a full pass every 10 minutes. A changed status or
            aiflow-ready label is recorded as a `ticket.status` event (the ticket timeline). Without a usable Jira
            credential it stops and keeps the jira_auth incident open (jira_connect.sync_gate).
watchdog    every 15 s: nodes without heartbeat, runs whose lease is gone, runs past the 30/45/60 minute clocks,
            accounts whose cooldown ended, nodes outside the version skew policy. Opens and closes incidents,
            alerts people on critical ones.
fleet       every 15 s: the rolling node upgrade toward CURRENT (canary, waves, halt, rollback; rollout.py).
merge_ready every 30 s: tickets in Ready to merge (or approved in Human review) are merged by the flow when their
            pull request can merge (merge_flow.py).
pr_reconcile  every 60 s: every ticket with a pull request is compared with GitHub; Jira follows whoever merged,
            closed or reopened it (merge_flow.py).
memory      every 60 s: tickets, finished run summaries and verify findings into memory (memory.index); Jira sync
            also records closed-ticket study cases once their runs finish. Run results capture their own records.
Each loop runs on one control-plane instance at a time (leader key in Redis).
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import environment as environment_reports
from . import ba_chat, escalations, jira_connect, knowledge, memory, merge_flow, notify, role_health, tooling
from . import projects as project_lifecycle
from . import rollout
from . import verify_retry
from . import epic_lifecycle
from . import version_release
from ..core.services import service_gaps
from ..core.toolchains import toolchain_gaps
from .incidents import check_idle_flow, check_stack_pins, close_incident, open_incident
from .api import Ctx, notify_human_review
from .db import J
from .repositories.runs import RunsRepository
from .state import NODE_OFFLINE_S
from .quota import assessments as quota_assessments
from ..core.quota import as_utc

log = logging.getLogger("aiflow4.loops")
OWNER = f"{socket.gethostname()}:{os.getpid()}"


def _reconcile_epics(ctx: Ctx, projects: list[str], changed_ticket_keys: set[str],
                     first_seen_ticket_keys: set[str], preexisting_ticket_keys: set[str]) -> None:
    """Apply table-guarded epic transitions through the shared lifecycle service."""
    epic_lifecycle.reconcile(ctx, projects, changed_ticket_keys, first_seen_ticket_keys, preexisting_ticket_keys)


def jira_sync(ctx: Ctx, full: bool = False) -> int:
    configured = ctx.repo().projects
    # The control repo is authoritative: paused or removed projects must stop being queried on the next pass.
    keys = sorted(key for key, project in configured.items() if not project.paused)
    # Without a usable credential (disconnected, refresh rejected), the jira_auth incident says why.
    if not keys or not jira_connect.sync_gate(ctx):
        return 0
    projects = ",".join(keys)
    jql = f"project in ({projects})" + ("" if full else " AND updated >= -5m")
    n = 0
    changed_ticket_keys: set[str] = set()
    first_seen_ticket_keys: set[str] = set()
    for project in keys:
        for version in ctx.jira.project_versions(project):
            ctx.db.run(
                "INSERT INTO versions (id, project, name, released, release_date)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET project=EXCLUDED.project,"
                " name=EXCLUDED.name, released=EXCLUDED.released, release_date=EXCLUDED.release_date",
                (version["id"], project, version["name"], version["released"], version["release_date"]))
    preexisting_ticket_keys = {
        row["key"]
        for project in keys
        for row in ctx.db.all("SELECT key FROM tickets WHERE project=%s", (project,))
    }
    # The sync reads each issue with its change history (Fix version attach dates, first In Progress).
    search = getattr(ctx.jira, "search_history", ctx.jira.search)
    for issue in search(jql + " ORDER BY updated DESC"):
        t = ctx.jira.normalize(issue)
        old = ctx.db.one("SELECT status, labels, parent_key FROM tickets WHERE key=%s", (t["key"],))
        if not old:
            first_seen_ticket_keys.add(t["key"])
        if (not old or old["parent_key"] != t["parent_key"] or
                (old["status"] == "Done" and t["status"] != "Done")):
            changed_ticket_keys.add(t["key"])
        done_at = t.get("done_at")
        if t["status"] == "Done" and done_at is None:
            done_at = t["jira_updated"]
        ctx.db.run(
            "INSERT INTO tickets (key, project, status, type, priority, labels, summary, description, parent_key, blocked_by,"
            " jira_created, jira_updated, done_at, fields, synced_at) VALUES (%(key)s,%(project)s,%(status)s,%(type)s,%(priority)s,%(labels)s,"
            " %(summary)s,%(description)s,%(parent_key)s,%(blocked_by)s,%(jira_created)s,%(jira_updated)s,%(done_at)s,%(fields)s, now())"
            " ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, type=EXCLUDED.type, priority=EXCLUDED.priority,"
            " labels=EXCLUDED.labels, summary=EXCLUDED.summary, description=EXCLUDED.description,"
            " parent_key=EXCLUDED.parent_key, blocked_by=EXCLUDED.blocked_by, jira_updated=EXCLUDED.jira_updated, fields=EXCLUDED.fields,"
            " done_at=CASE WHEN tickets.status <> 'Done' AND EXCLUDED.status = 'Done' THEN EXCLUDED.done_at"
            " WHEN tickets.done_at IS NULL AND EXCLUDED.status = 'Done' THEN EXCLUDED.done_at"
            " ELSE tickets.done_at END, synced_at=now()",
            {**t, "done_at": done_at, "fields": J({"assignee": t.get("assignee")})})
        # Replace the complete relationship for each ticket received from Jira, so removals are mirrored too.
        ctx.db.run(
            "WITH removed AS (DELETE FROM ticket_versions WHERE ticket_key=%s RETURNING ticket_key)"
            " INSERT INTO ticket_versions (ticket_key, version_id)"
            " SELECT %s, fix.version_id FROM unnest(%s::text[]) AS fix(version_id)"
            " CROSS JOIN (SELECT count(*) FROM removed) AS deletion_done"
            " ON CONFLICT (ticket_key, version_id) DO NOTHING",
            (t["key"], t["key"], t["fix_versions"]))
        # Jira change history: when each Fix version was attached, and the first move to In Progress.
        for version_id, added_at in (t.get("fix_version_added") or {}).items():
            ctx.db.run("INSERT INTO ticket_version_added (ticket_key, version_id, added_at) VALUES (%s,%s,%s)"
                       " ON CONFLICT (ticket_key, version_id) DO UPDATE SET added_at=EXCLUDED.added_at",
                       (t["key"], version_id, added_at))
        if t.get("first_in_progress"):
            ctx.db.run("UPDATE tickets SET first_in_progress_at=LEAST(first_in_progress_at, %s) WHERE key=%s",
                       (t["first_in_progress"], t["key"]))
        if not old:
            project_lifecycle.reopen_for_new_ticket(ctx, t["project"], t["key"])
        if t["type"] == "Epic" and t["status"] == "Done" and (not old or old["status"] != "Done"):
            try:
                project_lifecycle.report_done_epic(ctx, t["project"], t["key"])
            except Exception as e:  # noqa: BLE001
                log.warning("could not generate completion report for epic %s: %s", t["key"], e)
        if old and old["status"] != "Human review" and t["status"] == "Human review":
            notify_human_review(ctx, t["key"], t)
        if old and old["status"] != t["status"]:  # the flow writes its own moves to the mirror first: this one is a person's
            merge_flow.record_move(ctx, t["key"], old["status"], t["status"], merge_flow.PERSON, "moved in Jira")
        ready = "aiflow-ready" in t["labels"]
        if not old or old["status"] != t["status"] or ("aiflow-ready" in old["labels"]) != ready:
            # a move or a ready label made outside the control API (people, Jira automation): the ticket timeline
            ctx.events.emit("ticket.status", ticket=t["key"], project=t["project"], outcome=t["status"],
                            reason=f"from {old['status']}" if old else "first seen", from_status=old["status"] if old else "",
                            ready=ready, source="jira_sync")
        n += 1
    _reconcile_epics(ctx, keys, changed_ticket_keys, first_seen_ticket_keys, preexisting_ticket_keys)
    try:
        knowledge.capture_closed_tickets(ctx.db, ctx.settings.jira_site)
    except Exception as exc:  # noqa: BLE001 - knowledge capture must not stop Jira mirroring
        log.warning("closed ticket knowledge capture failed: %s", type(exc).__name__)
    return n


def watchdog(ctx: Ctx) -> None:
    """Run focused health checks for nodes, tickets, runs, accounts and verification exhaustion."""
    now = datetime.now(timezone.utc)
    repo = ctx.repo()
    from . import api as control_api

    _watch_nodes(ctx, now, repo, control_api.RELEASES)
    _watch_project_tools(ctx, repo)
    _watch_test_services(ctx, repo)
    _watch_tool_waits(ctx)
    _watch_nodes_without_accounts(ctx, repo)
    _watch_orphan_tickets(ctx)
    _watch_stuck_tickets(ctx)
    _check_verify_incidents(ctx, control_api.RELEASES)
    try:
        check_stack_pins(ctx)
        check_idle_flow(ctx)
    except Exception as e:  # noqa: BLE001 - a failing check must not stop the rest of the watchdog
        log.warning("idle and pin checks failed: %s", e)
    _watch_lost_runs(ctx)
    _watch_run_clocks(ctx, now, repo)
    _close_finished_run_incidents(ctx)
    _watch_account_state(ctx)
    _watch_role_coverage(ctx, repo)
    _watch_quota_limits(ctx)
    _watch_quota_forecasts(ctx)


def _watch_nodes(ctx: Ctx, now: datetime, repo, releases: Path) -> None:
    for node in ctx.db.all("SELECT name, status, last_heartbeat FROM nodes"):
        age = None if node["last_heartbeat"] is None else (now - node["last_heartbeat"]).total_seconds()
        if node["status"] == "online" and (age is None or age > NODE_OFFLINE_S):
            ctx.db.run("UPDATE nodes SET status='offline' WHERE name=%s", (node["name"],))
            ctx.events.emit("node.offline", node=node["name"], reason=f"no heartbeat for {int(age or 0)} s")
            open_incident(ctx, "node_down", node["name"], "critical", f"node {node['name']} is down",
                          f"No heartbeat for {int(age or 0)} seconds. Its runs lose their lease after 90 s and go back to To Do.")
        elif node["status"] == "online":
            close_incident(ctx, "node_down", node["name"], "heartbeat is back")
    rollout.watch_skew(ctx, releases)


def _watch_project_tools(ctx: Ctx, repo) -> None:
    tool_issues: set[str] = set()
    for node in ctx.db.all("SELECT name, status, tools, projects, dynamic FROM nodes"):
        for key in repo.projects_for_node(node["name"], node["projects"] or [], node["dynamic"]):
            project = repo.projects.get(key)
            if not project:
                continue
            subject = f"{node['name']}:{key}"
            if node["status"] != "online":
                tool_issues.add(subject)
                continue
            gaps = toolchain_gaps(node["tools"], project.tools, repo.versions, project.stacks,
                                  require_pins=bool(project.stacks))
            if gaps:
                tool_issues.add(subject)
                open_incident(ctx, "node_tools", subject, "warning", f"node {node['name']} misses tools for {key}",
                              f"Preflight on {node['name']} for {key} ({', '.join(project.stacks) or 'no stack'}) is unhealthy: "
                              f"{', '.join(gaps)}. The node agent installs stack tools and retries after a release or "
                              "toolchain update; the incident closes when a later heartbeat passes preflight.")
        _watch_node_ai_tools(ctx, repo, node, tool_issues)
    for incident in ctx.db.all("SELECT subject FROM incidents WHERE kind='node_tools' AND closed_at IS NULL"):
        if incident["subject"] not in tool_issues:
            close_incident(ctx, "node_tools", incident["subject"],
                           "tools are installed or the node no longer serves the project")


def _watch_node_ai_tools(ctx: Ctx, repo, node: dict, issues: set[str]) -> None:
    subject = f"{node['name']}:ai"
    missing = (toolchain_gaps(node["tools"], repo.provider_tools_for_node(node["name"]), repo.versions)
               if node["status"] == "online" else [])
    if node["status"] != "online" and repo.provider_tools_for_node(node["name"]):
        issues.add(subject)
    elif missing:
        issues.add(subject)
        open_incident(ctx, "node_tools", subject, "warning", f"node {node['name']} misses AI tools",
                      f"Missing on {node['name']} for its accounts: {', '.join(missing)}. Install them on the node; the"
                      " incident closes when a later heartbeat reports them.")


def _watch_test_services(ctx: Ctx, repo) -> None:
    service_issues: set[str] = set()
    for node in ctx.db.all("SELECT name, status, metrics, projects, dynamic FROM nodes WHERE status='online'"):
        for key in repo.projects_for_node(node["name"], node["projects"] or [], node["dynamic"]):
            project = repo.projects.get(key)
            gaps = service_gaps((node["metrics"] or {}).get("services"), project.required_services) if project else {}
            if not gaps:
                continue
            subject = f"{node['name']}:{key}"
            service_issues.add(subject)
            open_incident(ctx, "node_services", subject, "warning",
                          f"node {node['name']} has unhealthy test services for {key}",
                          f"{node['name']} takes no {key} work until its test services are healthy: "
                          + "; ".join(f"{service} ({why})" for service, why in gaps.items())
                          + ". The node agent recreates missing or stopped services; the incident closes when a later"
                          " heartbeat reports them healthy.")
    for incident in ctx.db.all("SELECT subject FROM incidents WHERE kind='node_services' AND closed_at IS NULL"):
        if incident["subject"] not in service_issues:
            close_incident(ctx, "node_services", incident["subject"],
                           "test services are healthy or the node no longer serves the project")


def _watch_tool_waits(ctx: Ctx) -> None:
    tooling.declare_undeclared_services(ctx)
    tooling.sweep(ctx)
    tooling.check_stalled_waits(ctx)
    environment_reports.sweep(ctx)
    environment_reports.scan_if_due(ctx)


def _watch_nodes_without_accounts(ctx: Ctx, repo) -> None:
    account_machines = {machine for account in repo.accounts for machine in account.machines}
    for node in ctx.db.all("SELECT name, status FROM nodes"):
        if node["status"] == "online" and node["name"] not in account_machines:
            where = "" if node["name"] in repo.nodes else " and is not described in aiflow-control/nodes/"
            open_incident(ctx, "node_no_ai", node["name"], "warning", f"node {node['name']} has no AI account",
                          f"{node['name']} is online{where}, but no AI account is assigned to it, so it cannot take any ticket."
                          " Assign accounts in aiflow-control/accounts.yaml and sign them in on the node.")
        elif node["name"] in account_machines or node["status"] != "online":
            close_incident(ctx, "node_no_ai", node["name"],
                           "accounts assigned" if node["name"] in account_machines else "node left")


def _watch_orphan_tickets(ctx: Ctx) -> None:
    if not ctx.jira:
        return
    query = "SELECT t.key FROM tickets t WHERE t.status='In Progress'"
    query += " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.status='running')"
    query += " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.started_at > now()-interval '10 minutes')"
    query += " AND t.synced_at > now()-interval '5 minutes'"
    for ticket in ctx.db.all(query):
        if ctx.leases.holder(ticket["key"]):
            continue
        try:
            if ctx.jira.status(ticket["key"]) != "In Progress":
                continue
            _requeue_orphan_ticket(ctx, ticket["key"])
        except Exception as e:  # noqa: BLE001
            log.warning("could not requeue orphan %s: %s", ticket["key"], e)


def _requeue_orphan_ticket(ctx: Ctx, key: str) -> None:
    ctx.jira.transition_to(key, "To Do")
    ctx.jira.comment(key, "AI Flow · watchdog · requeued\nWhat: The ticket was In Progress but no run had worked on it"
                     " for 10 minutes.\nWhy: A worker stopped between two updates, so nobody owned the ticket.\n"
                     "Next: Back to To Do; the next worker continues its branch.")
    ctx.db.run("UPDATE tickets SET status='To Do' WHERE key=%s", (key,))
    merge_flow.record_move(ctx, key, "In Progress", "To Do", merge_flow.FLOW,
                           "watchdog: In Progress without a run for 10 minutes")
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result) VALUES"
               " ('jira','transition',%s,'control',true,'To Do (orphan)')", (key,))
    ctx.events.emit("jira.transition", ticket=key, account="control", outcome="To Do",
                    reason="In Progress without a run for 10 minutes")
    ctx.events.emit("ticket.requeued", ticket=key, reason="In Progress without a run for 10 minutes")


def _watch_stuck_tickets(ctx: Ctx) -> None:
    stuck: dict[str, int] = {}
    for step in ("dev", "verify"):
        for row in RunsRepository(ctx.db).ticket_stuck_streaks(step):
            stuck[row["ticket_key"]] = max(stuck.get(row["ticket_key"], 0), row["fails"])
    for key, fails in stuck.items():
        if fails >= 3:
            last = ctx.db.one("SELECT account_id, summary FROM runs WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 1",
                              (key,))
            open_incident(ctx, "ticket_stuck", key, "warning", f"{key} failed {fails} runs in a row",
                          f"Last error ({last['account_id'] if last else '?'}): "
                          f"{(last['summary'] if last else '')[:300]}. Retries wait up to 30 minutes; "
                          "fix the cause or put the ticket on hold.")
    for incident in ctx.db.all("SELECT subject FROM incidents WHERE kind='ticket_stuck' AND closed_at IS NULL"):
        if incident["subject"].startswith("tool_wait:"):
            continue
        if stuck.get(incident["subject"], 0) < 3:
            close_incident(ctx, "ticket_stuck", incident["subject"], "a run produced work again")


def _watch_lost_runs(ctx: Ctx) -> None:
    for run in ctx.db.all("SELECT * FROM runs WHERE status='running'"):
        holder = ctx.leases.holder(run["ticket_key"])
        if holder == f"{run['id']}:{run['fence']}":
            continue
        _mark_lost_run(ctx, run, holder)


def _mark_lost_run(ctx: Ctx, run: dict, holder: str | None) -> None:
    key = run["ticket_key"]
    ctx.db.run("UPDATE runs SET status='lost', outcome='lost', ended_at=now() WHERE id=%s", (run["id"],))
    ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'",
               (run["account_id"],))
    ctx.events.emit("run.lost", ticket=key, run_id=str(run["id"]), fence=run["fence"], account=run["account_id"],
                    node=run["node"], reason="lease expired")
    if not ctx.jira or holder:
        return
    try:
        if ctx.jira.status(key) != "In Progress":
            return
        ctx.jira.transition_to(key, "To Do")
        ctx.jira.comment(key, f"AI Flow: the run on {run['node']} ({run['account_id']}) stopped sending heartbeats; "
                         "the ticket is back in To Do and keeps its branch for the next run.")
        _audit_lost_run(ctx, run)
    except Exception as e:  # noqa: BLE001
        log.warning("could not requeue %s: %s", key, e)


def _audit_lost_run(ctx: Ctx, run: dict) -> None:
    key = run["ticket_key"]
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result)"
               " VALUES ('jira','transition',%s,%s,%s,'control',true,'To Do (lease lost)')",
               (key, str(run["id"]), run["fence"]))
    ctx.db.run("UPDATE tickets SET status='To Do' WHERE key=%s", (key,))
    merge_flow.record_move(ctx, key, "In Progress", "To Do", merge_flow.FLOW,
                           f"watchdog: the run on {run['node']} lost its lease")
    ctx.events.emit("jira.transition", ticket=key, run_id=str(run["id"]), fence=run["fence"],
                    account="control", node=run["node"], outcome="To Do", reason="lease lost")


def _watch_run_clocks(ctx: Ctx, now: datetime, repo) -> None:
    for run in ctx.db.all("SELECT * FROM runs WHERE status='running'"):
        project = repo.projects.get(run["project"])
        clocks = project.work_minutes() if project else {"target": 30, "warn": 45, "hard": 60}
        minutes = (now - run["started_at"]).total_seconds() / 60
        if minutes >= clocks["hard"]:
            open_incident(ctx, "run_hard_limit", run["ticket_key"], "critical",
                          f"{run['ticket_key']} passed {clocks['hard']} minutes",
                          f"Run {run['id']} on {run['node']} ({run['account_id']}) has worked {int(minutes)} minutes.")
        elif minutes >= clocks["warn"]:
            open_incident(ctx, "run_slow", run["ticket_key"], "warning",
                          f"{run['ticket_key']} passed {clocks['warn']} minutes",
                          f"Run {run['id']} on {run['node']} ({run['account_id']}) has worked {int(minutes)} minutes.")


def _close_finished_run_incidents(ctx: Ctx) -> None:
    for kind in ("run_slow", "run_hard_limit"):
        for incident in ctx.db.all("SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (kind,)):
            running = ctx.db.one("SELECT 1 FROM runs WHERE ticket_key=%s AND status='running'", (incident["subject"],))
            if not running:
                close_incident(ctx, kind, incident["subject"], "run finished")


def _watch_account_state(ctx: Ctx) -> None:
    for account in ctx.db.all("SELECT id, status, cooldown_until FROM accounts WHERE status='cooldown'"):
        if not ctx.leases.cooling(account["id"]):
            ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s", (account["id"],))
            close_incident(ctx, "account_quota", account["id"], "cooldown ended")
    for account in ctx.db.all("SELECT id, provider, status, cooldown_until, last_error FROM accounts"
                              " WHERE status IN ('cooldown','signed_out','error')"):
        what = {"cooldown": "is out of quota", "signed_out": "is signed out", "error": "fails its check"}[account["status"]]
        until = f" until {account['cooldown_until']:%Y-%m-%d %H:%M} UTC" if account["cooldown_until"] else ""
        open_incident(ctx, "account_quota", account["id"], "warning", f"account {account['id']} {what}",
                      f"{account['provider']}: {account['last_error'][:300]}{until}. Its tickets go to the other accounts.")
    for account in ctx.db.all("SELECT id FROM accounts WHERE status IN ('idle','working')"):
        close_incident(ctx, "account_quota", account["id"], "account works again")


def _watch_role_coverage(ctx: Ctx, repo) -> None:
    online = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE status='online'")}
    by_id = {account.id: account for account in repo.accounts}
    ready = {row["id"] for row in ctx.db.all("SELECT id FROM accounts WHERE status IN ('idle','working','probing')")
             if row["id"] in by_id and online.intersection(by_id[row["id"]].machines)}
    try:
        role_health.evaluate(ctx)
    except Exception as e:  # noqa: BLE001
        log.warning("role health evaluation failed: %s", e)
    switched_off = role_health.disabled_roles(ctx.db)
    for role in ("master", "intake_refiner", "verify", "dev"):
        holders = [account for account in repo.accounts if role in (account.roles or [])]
        if not holders:
            continue
        available = [account.id for account in holders if account.id in ready and (account.id, role) not in switched_off]
        if len(available) < 2:
            severity = "critical" if not available else "warning"
            open_incident(ctx, "role_coverage", role, severity,
                          f"role {role} has {len(available)} ready account{'s' if len(available) != 1 else ''}",
                          f"Accounts with the role: {', '.join(account.id for account in holders)}; "
                          f"ready: {', '.join(available) or 'none'}. Every role needs two ready accounts of different "
                          "providers so one quota or outage cannot stop the flow.")
        else:
            close_incident(ctx, "role_coverage", role, f"{len(available)} accounts ready")


def _watch_quota_limits(ctx: Ctx) -> None:
    for account in ctx.db.all("SELECT id, provider, quota FROM accounts WHERE quota ? 'windows'"):
        for window, data in (account["quota"].get("windows") or {}).items():
            utilization = data.get("utilization")
            if utilization is None:
                continue
            _watch_quota_window(ctx, account, window, float(utilization), data.get("resets_at"))


def _watch_quota_window(ctx: Ctx, account: dict, window: str, utilization: float, reset) -> None:
    reset_at = as_utc(reset)
    reset_text = reset_at.strftime("%Y-%m-%d %H:%M UTC") if reset_at else "unknown"
    label = {"five_hour": "5-hour", "seven_day": "weekly"}.get(window, window)
    subject = f"{account['id']}:{window}"
    if utilization >= 0.9:
        severity = "critical" if utilization >= 1.0 else "warning"
        tip = (" If the account has a limit reset offer, Settings > Usage > Reset for free restores it now."
               if account["provider"] == "claude" and window == "seven_day" else "")
        open_incident(ctx, "quota_high", subject, severity,
                      f"{account['id']} {label} limit at {round(utilization * 100)}%",
                      f"Resets at {reset_text}. At 100% the account pauses until then and other accounts take"
                      f" its tickets.{tip}")
    elif utilization < 0.85:
        close_incident(ctx, "quota_high", subject, f"{label} usage back to {round(utilization * 100)}%")


def _watch_quota_forecasts(ctx: Ctx) -> None:
    account_rows = ctx.db.all("SELECT id, provider, quota FROM accounts ORDER BY id")
    providers = {str(row["id"]): str(row["provider"]) for row in account_rows}
    for account_id, awareness in quota_assessments(ctx.db, account_rows).items():
        at_risk = [window for window in awareness["windows"]
                   if window["prediction"]["status"] == "will run out before reset"]
        ctx.db.run("UPDATE accounts SET quota_at_risk=%s,quota_forecast=%s WHERE id=%s",
                   (bool(at_risk), J(awareness), account_id))
        if at_risk:
            _open_quota_prediction(ctx, account_id, providers, awareness, at_risk)
        else:
            close_incident(ctx, "quota_prediction", account_id, "quota forecast is no longer before reset")


def _open_quota_prediction(ctx: Ctx, account_id: str, providers: dict, awareness: dict, at_risk: list[dict]) -> None:
    soonest = min((window["prediction"].get("exhaustion_at") or "") for window in at_risk)
    windows = ", ".join(window["label"] for window in at_risk)
    open_incident(ctx, "quota_prediction", account_id, "warning", f"{account_id} may run out before reset",
                  f"Predicted exhaustion: {soonest or 'before reset'} UTC ({windows}). "
                  f"Confidence: {awareness['confidence']}. New work is routed to other "
                  f"{providers.get(account_id, 'AI')} accounts first.")


def _check_verify_incidents(ctx: Ctx, releases) -> None:
    try:
        from .candidate_runtime import initialize as initialize_candidate_runtime
        initialize_candidate_runtime(ctx, releases)
        verify_retry.check_incidents(ctx, releases)
    except Exception as exc:  # noqa: BLE001 - verification exhaustion must not stop other watchdog work
        log.warning("verify no-verdict check failed: %s", exc)


def fleet_rollout(ctx: Ctx) -> dict | None:
    """The rolling node upgrade toward CURRENT: health checks, next wave, halt and rollback (rollout.tick)."""
    from . import api as control_api
    return rollout.tick(ctx, rollout.read_current(control_api.RELEASES))


def version_release_scan(ctx: Ctx) -> int:
    """Refresh live Jira version membership and audit which configured versions are ready or blocked."""
    from . import api as control_api
    control_api.github_gateway(ctx)  # merge-back retries for released versions need GitHub
    return version_release.scan(ctx)


def reconcile_loop(ctx: Ctx, stop: threading.Event) -> None:
    """Compare every ticket that has a pull request with GitHub, a pass every merge_flow.RECONCILE_EVERY_S."""
    from . import api as control_api
    while not stop.is_set():
        started = time.monotonic()
        try:
            if ctx.leases.leader("pr_reconcile", OWNER, 60_000):
                control_api.github_gateway(ctx)  # connect GitHub when the secret store has a connection
                merge_flow.reconcile(ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("reconcile loop error: %s", e)
            ctx.events.emit("control.error", reason=str(e)[:300])
        stop.wait(max(1.0, merge_flow.RECONCILE_EVERY_S - (time.monotonic() - started)))


def run_loops(ctx: Ctx, stop: threading.Event | None = None) -> None:
    from . import support_bundle

    support_bundle.control_log_buffer(ctx.settings.support_log_dir, ctx.settings.o2_source)
    stop = stop or threading.Event()
    last_sync = last_full = last_wd = last_mem = last_rollout = last_release = last_backup_jobs = last_ba_chat = 0.0
    last_merge = 0.0
    from . import api as control_api
    ctx.events.start()
    # Jira sync, the watchdog and the merge step can be slow: the reconcile loop has its own thread so that they
    # never delay the one-minute bound of "Jira follows GitHub" (merge_flow.RECONCILE_BOUND_S)
    reconciler = threading.Thread(target=reconcile_loop, args=(ctx, stop), name="pr-reconcile", daemon=True)
    reconciler.start()
    while not stop.is_set():
        now = time.time()
        try:
            if now - last_sync >= 20 and ctx.leases.leader("jira_sync", OWNER, 60_000):
                full = now - last_full >= 600
                n = jira_sync(ctx, full=full)
                if full:
                    last_full = now
                    log.info("jira full sync: %s tickets", n)
                last_sync = now
            if now - last_wd >= 15 and ctx.leases.leader("watchdog", OWNER, 45_000):
                watchdog(ctx)
                escalations.retry_resumptions(ctx)
                notify.process_retries(ctx)
                notify.send_scheduled_summaries(ctx)
                last_wd = now
            if now - last_ba_chat >= 5 and ctx.leases.leader("ba_chat_poll", OWNER, 30_000):
                ba_chat.poll(ctx)
                last_ba_chat = now
            if now - last_merge >= merge_flow.MERGE_EVERY_S and ctx.leases.leader("merge_ready", OWNER, 60_000):
                control_api.github_gateway(ctx)  # connect GitHub when the secret store has a connection
                for key in merge_flow.merge_ready(ctx):
                    log.info("merged %s", key)
                last_merge = now
            if now - last_rollout >= 15 and ctx.leases.leader("fleet_rollout", OWNER, 45_000):
                fleet_rollout(ctx)
                last_rollout = now
            if now - last_release >= 30 and ctx.leases.leader("version_release_scan", OWNER, 60_000):
                version_release_scan(ctx)
                last_release = now
            if now - last_mem >= 60 and ctx.leases.leader("memory_index", OWNER, 120_000):
                written = memory.index(ctx.db)
                if any(written.values()):
                    log.info("memory indexed: %s", written)
                last_mem = now
            if now - last_backup_jobs >= 60 and ctx.leases.leader("backup_jobs", OWNER, 14_400_000):
                from . import backups as backup_jobs
                backup_jobs.scheduled_backup(ctx)
                backup_jobs.weekly_restore_test(ctx)
                last_backup_jobs = now
        except Exception as e:  # noqa: BLE001
            log.exception("loop error: %s", e)
            ctx.events.emit("control.error", reason=str(e)[:300])
        stop.wait(2)
