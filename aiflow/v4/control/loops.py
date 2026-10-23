"""Background loops of the control plane (process `aiflow4 control-loops`).

jira_sync   every 20 s: tickets updated in the last 5 minutes; a full pass every 10 minutes. A changed status or
            aiflow-ready label is recorded as a `ticket.status` event (the ticket timeline). Without a usable Jira
            credential it stops and keeps the jira_auth incident open (jira_connect.sync_gate).
watchdog    every 15 s: nodes without heartbeat, runs whose lease is gone, runs past the 30/45/60 minute clocks,
            accounts whose cooldown ended, nodes outside the version skew policy. Opens and closes incidents,
            alerts people on critical ones.
fleet       every 15 s: the rolling node upgrade toward CURRENT (canary, waves, halt, rollback; rollout.py).
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
from urllib.parse import quote

from . import escalations, jira_connect, knowledge, memory, notify, role_health, tooling
from . import projects as project_lifecycle
from . import rollout
from . import version_release
from ..core.services import service_gaps
from ..core.toolchains import toolchain_gaps
from .incidents import check_idle_flow, check_stack_pins, close_incident, open_incident
from .api import Ctx, notify_human_review
from .db import J
from .state import NODE_OFFLINE_S
from .quota import assessments as quota_assessments
from ..core.quota import as_utc

log = logging.getLogger("aiflow4.loops")
OWNER = f"{socket.gethostname()}:{os.getpid()}"


def _is_under_epic(ticket: dict, epic_key: str, by_key: dict[str, dict]) -> bool:
    parent = ticket.get("parent_key")
    seen: set[str] = set()
    while parent and parent not in seen:
        if parent == epic_key:
            return True
        seen.add(parent)
        parent_ticket = by_key.get(parent)
        parent = parent_ticket.get("parent_key") if parent_ticket else None
    return False


def _reconcile_epics(ctx: Ctx, projects: list[str], changed_ticket_keys: set[str],
                     first_seen_ticket_keys: set[str], preexisting_ticket_keys: set[str]) -> None:
    """Close epics whose mirrored children are all Done, and reopen on newly active child work."""
    for project in projects:
        rows = ctx.db.all("SELECT key, project, status, type, parent_key, labels FROM tickets WHERE project=%s ORDER BY key",
                          (project,))
        by_key = {row["key"]: row for row in rows}
        epics = [row for row in rows if row["type"] == "Epic"]
        for epic in epics:
            children = [row for row in rows if row["type"] != "Epic" and _is_under_epic(row, epic["key"], by_key)]
            if not children:
                continue
            all_done = all(child["status"] == "Done" for child in children)
            if all_done:
                target = "Done"
            elif epic["status"] == "Done" and any(
                    child["key"] in changed_ticket_keys and child["status"] != "Done" and
                    (child["key"] not in first_seen_ticket_keys or epic["key"] in preexisting_ticket_keys)
                    for child in children):
                target = "To Do"
            else:
                continue
            if epic["status"] == target:
                continue
            try:
                previous_status = epic["status"]
                current = ctx.jira.status(epic["key"])
                moved_by_flow = current != target
                status = ctx.jira.transition_to(epic["key"], target) if moved_by_flow else current
                if status != target:
                    continue
                now = datetime.now(timezone.utc)
                ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done'"
                           " THEN %s ELSE done_at END, synced_at=now() WHERE key=%s",
                           (status, status, now, epic["key"]))
                epic["status"] = status
                ready = "aiflow-ready" in (epic.get("labels") or [])
                ctx.events.emit("ticket.status", ticket=epic["key"], project=project, outcome=status,
                                reason=f"from {previous_status}", from_status=previous_status, ready=ready,
                                source="jira_sync")
                if target == "Done":
                    try:
                        project_lifecycle.report_done_epic(ctx, project, epic["key"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("could not generate completion report for epic %s: %s", epic["key"], e)
                if not moved_by_flow:
                    continue
                reason = (f"all {len(children)} child tickets are Done" if target == "Done"
                          else f"{len(children)} child tickets are not all Done")
                ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
                           " VALUES ('jira','transition',%s,'control',true,%s,%s)",
                           (epic["key"], status, J({"source": "jira_sync", "reason": reason,
                                                    "child_count": len(children)})))
                ctx.events.emit("jira.transition", ticket=epic["key"], account="control", outcome=status,
                                reason=reason)
                if target == "Done":
                    report = (ctx.settings.public_url.rstrip("/") + f"/v1/projects/{quote(project, safe='')}/report"
                              f"?epic={quote(epic['key'], safe='')}&format=html")
                    comment = ("AI Flow · epic completed\n"
                               f"Child tickets: {len(children)} (all Done)\n"
                               f"Completed at: {now.isoformat(timespec='seconds')}\n"
                               f"Report: {report}")
                    project_done = bool(epics) and all(
                        row["status"] == "Done" or row["key"] == epic["key"] for row in epics)
                    if project_done:
                        project_report = (ctx.settings.public_url.rstrip("/") +
                                          f"/v1/projects/{quote(project, safe='')}/report?format=html")
                        comment += (f"\nProject suggestion: all {len(epics)} epics are Done; "
                                    f"consider closing project {project}. Report: {project_report}")
                    ctx.jira.comment(epic["key"], comment)
            except Exception as e:  # noqa: BLE001
                log.warning("could not reconcile epic %s: %s", epic["key"], e)


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
    now = datetime.now(timezone.utc)
    # nodes
    for n in ctx.db.all("SELECT name, status, last_heartbeat FROM nodes"):
        age = None if n["last_heartbeat"] is None else (now - n["last_heartbeat"]).total_seconds()
        if n["status"] == "online" and (age is None or age > NODE_OFFLINE_S):
            ctx.db.run("UPDATE nodes SET status='offline' WHERE name=%s", (n["name"],))
            ctx.events.emit("node.offline", node=n["name"], reason=f"no heartbeat for {int(age or 0)} s")
            open_incident(ctx, "node_down", n["name"], "critical", f"node {n['name']} is down",
                          f"No heartbeat for {int(age or 0)} seconds. Its runs lose their lease after 90 s and go back to To Do.")
        elif n["status"] == "online":
            close_incident(ctx, "node_down", n["name"], "heartbeat is back")
    # version skew: a node outside the control/node policy gets no new work until it runs a compatible release
    from . import api as control_api
    rollout.watch_skew(ctx, control_api.RELEASES)
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
    # tickets waiting for a tool no machine has: the 24 hour clock and the one maintenance ticket per tool
    tooling.sweep(ctx)
    tooling.check_stalled_waits(ctx)
    # nodes that joined but have no AI account: they add slots, never work
    with_accounts = {m for a in repo.accounts for m in a.machines}
    for n in ctx.db.all("SELECT name, status FROM nodes"):
        if n["status"] == "online" and n["name"] not in with_accounts:
            where = "" if n["name"] in repo.nodes or repo.inventory_in_db else " and is not described in aiflow-control/nodes/"
            open_incident(ctx, "node_no_ai", n["name"], "warning", f"node {n['name']} has no AI account",
                          f"{n['name']} is online{where}, but no AI account is assigned to it, so it cannot take any ticket."
                          " Assign accounts in aiflow-control/accounts.yaml and sign them in on the node.")
        elif n["name"] in with_accounts or n["status"] != "online":
            close_incident(ctx, "node_no_ai", n["name"], "accounts assigned" if n["name"] in with_accounts else "node left")
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
                ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result) VALUES"
                           " ('jira','transition',%s,'control',true,'To Do (orphan)')", (t["key"],))
                ctx.events.emit("jira.transition", ticket=t["key"], account="control", outcome="To Do",
                                reason="In Progress without a run for 10 minutes")
                ctx.events.emit("ticket.requeued", ticket=t["key"], reason="In Progress without a run for 10 minutes")
            except Exception as e:  # noqa: BLE001
                log.warning("could not requeue orphan %s: %s", t["key"], e)
    # tickets whose dev runs keep failing (backoff in the API keeps them from spinning; a person should look)
    from .api import BACKOFF_SQL
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
    # a flow that stands still and a configuration that keeps a project from getting work
    try:
        check_stack_pins(ctx)
        check_idle_flow(ctx)
    except Exception as e:  # noqa: BLE001 - a failing check must not stop the rest of the watchdog
        log.warning("idle and pin checks failed: %s", e)
    # runs whose lease disappeared (worker died, node down, network split)
    for r in ctx.db.all("SELECT * FROM runs WHERE status='running'"):
        holder = ctx.leases.holder(r["ticket_key"])
        if holder != f"{r['id']}:{r['fence']}":
            ctx.db.run("UPDATE runs SET status='lost', outcome='lost', ended_at=now() WHERE id=%s", (r["id"],))
            ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'", (r["account_id"],))
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
                        ctx.events.emit("jira.transition", ticket=r["ticket_key"], run_id=str(r["id"]), fence=r["fence"],
                                        account="control", node=r["node"], outcome="To Do", reason="lease lost")
                except Exception as e:  # noqa: BLE001
                    log.warning("could not requeue %s: %s", r["ticket_key"], e)
            continue
        # clocks: 30 target, 45 warn, 60 hard (per project)
        p = ctx.repo().projects.get(r["project"])
        wm = p.work_minutes() if p else {"target": 30, "warn": 45, "hard": 60}
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
    for role in ("master", "intake_refiner", "verify", "dev"):
        holders = [a for a in repo.accounts if role in (a.roles or [])]
        ok = [a.id for a in holders if a.id in ready and (a.id, role) not in switched_off]
        if not holders:
            continue
        if len(ok) < 2:
            open_incident(ctx, "role_coverage", role, "critical" if not ok else "warning",
                          f"role {role} has {len(ok)} ready account{'s' if len(ok) != 1 else ''}",
                          f"Accounts with the role: {', '.join(a.id for a in holders)}; ready: {', '.join(ok) or 'none'}."
                          " Every role needs two ready accounts of different providers so one quota or outage cannot stop the flow.")
        else:
            close_incident(ctx, "role_coverage", role, f"{len(ok)} accounts ready")
    # quota windows reported by the provider (Claude: five_hour and seven_day utilization with reset times)
    for a in ctx.db.all("SELECT id, provider, quota FROM accounts WHERE quota ? 'windows'"):
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

    account_rows = ctx.db.all("SELECT id, provider, quota FROM accounts ORDER BY id")
    account_providers = {str(row["id"]): str(row["provider"]) for row in account_rows}
    for account_id, awareness in quota_assessments(ctx.db, account_rows).items():
        at_risk = [w for w in awareness["windows"]
                   if w["prediction"]["status"] == "will run out before reset"]
        ctx.db.run("UPDATE accounts SET quota_at_risk=%s WHERE id=%s", (bool(at_risk), account_id))
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


def fleet_rollout(ctx: Ctx) -> dict | None:
    """The rolling node upgrade toward CURRENT: health checks, next wave, halt and rollback (rollout.tick)."""
    from . import api as control_api
    return rollout.tick(ctx, rollout.read_current(control_api.RELEASES))


def version_release_scan(ctx: Ctx) -> int:
    """Refresh live Jira version membership and audit which configured versions are ready or blocked."""
    from . import api as control_api
    control_api.github_gateway(ctx)  # merge-back retries for released versions need GitHub
    return version_release.scan(ctx)


def run_loops(ctx: Ctx, stop: threading.Event | None = None) -> None:
    from . import support_bundle

    support_bundle.control_log_buffer(ctx.settings.support_log_dir, ctx.settings.o2_source)
    stop = stop or threading.Event()
    last_sync = last_full = last_wd = last_mem = last_rollout = last_release = last_backup_jobs = 0.0
    ctx.events.start()
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
                from ..worker import backups as backup_worker
                backup_worker.scheduled_backup(ctx)
                backup_worker.weekly_restore_test(ctx)
                last_backup_jobs = now
        except Exception as e:  # noqa: BLE001
            log.exception("loop error: %s", e)
            ctx.events.emit("control.error", reason=str(e)[:300])
        stop.wait(2)
