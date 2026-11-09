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

from . import ba_chat, escalations, jira_connect, knowledge, memory, merge_flow, notify, owner_notifications
from . import flow_doctor
from . import epic_lifecycle
from . import system_check
from . import rollout
from . import projects as project_lifecycle
from . import version_release
from .application_context import ApplicationContext
from .db import J
from aiflow.v4.control.features.tickets import ticket_repository_of

log = logging.getLogger("aiflow4.loops")
OWNER = f"{socket.gethostname()}:{os.getpid()}"


def _reconcile_epics(ctx: ApplicationContext, projects: list[str], changed_ticket_keys: set[str],
                     first_seen_ticket_keys: set[str], preexisting_ticket_keys: set[str]) -> None:
    """Reconcile child-driven epic changes through the shared lifecycle module."""
    epic_lifecycle.reconcile(ctx, projects, changed_ticket_keys, first_seen_ticket_keys, preexisting_ticket_keys)


def _mirror_versions(ctx: ApplicationContext, keys: list[str]) -> None:
    """Mirror the Jira versions of each project."""
    for project in keys:
        for version in ctx.jira.project_versions(project):
            ctx.db.run(
                "INSERT INTO versions (id, project, name, released, release_date)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET project=EXCLUDED.project,"
                " name=EXCLUDED.name, released=EXCLUDED.released, release_date=EXCLUDED.release_date",
                (version["id"], project, version["name"], version["released"], version["release_date"]))


def _mirror_ticket(ctx: ApplicationContext, t: dict, done_at) -> None:
    """Upsert a ticket read from Jira, replace its Fix versions and record when they were attached."""
    ticket_repository_of(ctx).upsert_mirrored_ticket_row({**t, 'done_at': done_at,
        'fields': J({'assignee': t.get('assignee'),
                     'parent_changed_at': t['parent_changed_at'].isoformat() if t.get('parent_changed_at') else None})})
    # Replace the complete relationship for each ticket received from Jira, so removals are mirrored too.
    ticket_repository_of(ctx).replace_ticket_version_links((t['key'], t['key'], t['fix_versions']))
    # Jira change history: when each Fix version was attached, and the first move to In Progress.
    for version_id, added_at in (t.get("fix_version_added") or {}).items():
        ticket_repository_of(ctx).record_ticket_version_added((t["key"], version_id, added_at))
    if t.get("first_in_progress"):
        ticket_repository_of(ctx).earliest_first_in_progress_at((t['first_in_progress'], t['key']))


def _report_newly_mirrored_epic(ctx: ApplicationContext, ticket: dict, old: dict | None) -> None:
    """Report an epic imported already Done, preserving the Jira-sync side effect."""
    if ticket["type"] != "Epic" or ticket["status"] != "Done" or old:
        return
    try:
        project_lifecycle.report_done_epic(ctx, ticket["project"], ticket["key"])
    except Exception as error:  # noqa: BLE001 - reporting must not stop Jira mirroring
        log.warning("could not generate completion report for epic %s: %s", ticket["key"], error)


def _sync_jira_issue(ctx: ApplicationContext, issue: dict, jira_flow_actor: str,
                     changed_ticket_keys: set[str], first_seen_ticket_keys: set[str]) -> dict | None:
    """Mirror one Jira issue and record its status, hierarchy and operator changes."""
    ticket = ctx.jira.normalize(issue)
    old = ticket_repository_of(ctx).get_ticket_sync_fields((ticket['key'],))
    if old and jira_flow_actor:
        for change in ctx.jira.operator_changes(issue, old.get("jira_updated"), jira_flow_actor):
            flow_doctor.record_operator_intervention(
                ctx, source_key=change["source_key"], source="jira", actor=change["actor"], action="edit",
                target=change["target"], happened_at=change["happened_at"],
                reason="Jira history records a change by an account outside AI Flow",
                details={"fields": change["fields"]})
    if not old:
        first_seen_ticket_keys.add(ticket["key"])
    if (not old or old["parent_key"] != ticket["parent_key"] or
            (old["status"] == "Done" and ticket["status"] != "Done")):
        changed_ticket_keys.add(ticket["key"])
    done_at = ticket.get("done_at")
    if ticket["status"] == "Done" and done_at is None:
        done_at = ticket["jira_updated"]
    _mirror_ticket(ctx, ticket, done_at)
    if not old:
        project_lifecycle.reopen_for_new_ticket(ctx, ticket["project"], ticket["key"])
    if old and old["status"] != ticket["status"]:
        merge_flow.record_move(ctx, ticket["key"], old["status"], ticket["status"], merge_flow.PERSON,
                               "moved in Jira", at=ticket.get("status_changed_at"))
    ready = "aiflow-ready" in ticket["labels"]
    if not old or old["status"] != ticket["status"] or ("aiflow-ready" in old["labels"]) != ready:
        ctx.events.emit("ticket.status", ticket=ticket["key"], project=ticket["project"],
                        outcome=ticket["status"], reason=f"from {old['status']}" if old else "first seen",
                        from_status=old["status"] if old else "", ready=ready, source="jira_sync")
    return ticket if ticket["type"] == "Epic" and ticket["status"] == "Done" and not old else None


def jira_sync(ctx: ApplicationContext, full: bool = False) -> int:
    """Mirror recently changed Jira issues and reconcile their epic state."""
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
    _mirror_versions(ctx, keys)
    preexisting_ticket_keys = {
        row["key"]
        for project in keys
        for row in ticket_repository_of(ctx).list_ticket_keys_in_project((project,))
    }
    # The sync reads each issue with its change history (Fix version attach dates, first In Progress).
    search = getattr(ctx.jira, "search_history", ctx.jira.search)
    try:
        jira_flow_actor = ctx.jira.current_account_id()
    except Exception:  # noqa: BLE001 - no authenticated identity means external history cannot be classified safely
        jira_flow_actor = ""
    issues = list(search(jql + " ORDER BY updated DESC"))
    newly_done_epics = []
    repository = ticket_repository_of(ctx)
    # Apply the completed Jira result set atomically so the watchdog cannot read a partial descendant tree.
    with ctx.db.transaction():
        for epic_key in repository.epic_keys_for_projects(keys):
            repository.lock_epic_lifecycle(epic_key)
        for issue in issues:
            newly_done = _sync_jira_issue(ctx, issue, jira_flow_actor,
                                          changed_ticket_keys, first_seen_ticket_keys)
            if newly_done:
                newly_done_epics.append(newly_done)
            n += 1
    for ticket in newly_done_epics:
        _report_newly_mirrored_epic(ctx, ticket, None)
    _reconcile_epics(ctx, keys, changed_ticket_keys, first_seen_ticket_keys, preexisting_ticket_keys)
    try:
        knowledge.capture_closed_tickets(ctx.db, ctx.settings.jira_site, ticket_repository=ticket_repository_of(ctx))
    except Exception as exc:  # noqa: BLE001 - knowledge capture must not stop Jira mirroring
        log.warning("closed ticket knowledge capture failed: %s", type(exc).__name__)
    return n


def watchdog(ctx: ApplicationContext) -> None:
    from . import watchdog as watchdog_checks
    watchdog_checks.run(ctx)


def fleet_rollout(ctx: ApplicationContext) -> dict | None:
    """The rolling node upgrade toward CURRENT: health checks, next wave, halt and rollback (rollout.tick)."""
    from . import api as control_api
    return rollout.tick(ctx, rollout.read_current(control_api.RELEASES))


def version_release_scan(ctx: ApplicationContext) -> int:
    """Refresh live Jira version membership and audit which configured versions are ready or blocked."""
    from .gateway_helpers import github_gateway
    github_gateway(ctx)  # merge-back retries for released versions need GitHub
    return version_release.scan(ctx)


def reconcile_loop(ctx: ApplicationContext, stop: threading.Event) -> None:
    """Compare every ticket that has a pull request with GitHub, a pass every merge_flow.RECONCILE_EVERY_S."""
    from .gateway_helpers import github_gateway
    while not stop.is_set():
        started = time.monotonic()
        try:
            if ctx.leases.leader("pr_reconcile", OWNER, 60_000):
                github_gateway(ctx)  # connect GitHub when the secret store has a connection
                merge_flow.reconcile(ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("reconcile loop error: %s", e)
            ctx.events.emit("control.error", reason=str(e)[:300])
        stop.wait(max(1.0, merge_flow.RECONCILE_EVERY_S - (time.monotonic() - started)))


def run_loops(ctx: ApplicationContext, stop: threading.Event | None = None) -> None:
    from .gateway_helpers import github_gateway
    from . import support_bundle

    support_bundle.control_log_buffer(ctx.settings.support_log_dir, ctx.settings.o2_source)
    stop = stop or threading.Event()
    last_sync = last_full = last_wd = last_mem = last_rollout = last_release = last_backup_jobs = 0.0
    last_doctor = last_system_check = last_ba_chat = 0.0
    last_merge = 0.0
    ctx.events.start()
    # Jira sync, the watchdog and the merge step can be slow: the reconcile loop has its own thread so that they
    # never delay the one-minute bound of "Jira follows GitHub" (merge_flow.RECONCILE_BOUND_S)
    reconciler = threading.Thread(target=reconcile_loop, args=(ctx, stop), name="pr-reconcile", daemon=True)
    reconciler.start()
    from .platform.events import wiring as event_wiring
    threading.Thread(target=event_wiring.dispatch_loop, args=(ctx, stop), name="event-dispatch", daemon=True).start()
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
                owner_notifications.process_pending(ctx)
                notify.process_retries(ctx)
                notify.send_scheduled_summaries(ctx)
                last_wd = now
            if now - last_ba_chat >= 5 and ctx.leases.leader("ba_chat_poll", OWNER, 30_000):
                ba_chat.poll(ctx)
                last_ba_chat = now
            if now - last_merge >= merge_flow.MERGE_EVERY_S and ctx.leases.leader("merge_ready", OWNER, 60_000):
                github_gateway(ctx)  # connect GitHub when the secret store has a connection
                for key in merge_flow.merge_ready(ctx):
                    log.info("merged %s", key)
                last_merge = now
            if now - last_rollout >= 15 and ctx.leases.leader("fleet_rollout", OWNER, 45_000):
                fleet_rollout(ctx)
                last_rollout = now
            if now - last_release >= 30 and ctx.leases.leader("version_release_scan", OWNER, 60_000):
                version_release_scan(ctx)
                last_release = now
            if now - last_system_check >= 3600 and ctx.leases.leader("system_check", OWNER, 3_700_000):
                system_check.refresh_all(ctx, "hourly")
                last_system_check = now
            if now - last_mem >= 60 and ctx.leases.leader("memory_index", OWNER, 120_000):
                written = memory.index(ctx.db, ticket_repository_of(ctx))
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
        if now - last_doctor >= 60 and ctx.leases.leader("flow_doctor", OWNER, 70_000):
            last_doctor = now  # a failed pass still observes the minute cadence, rather than retrying every 2 s
            try:
                report = flow_doctor.tick(ctx)
                log.info("flow doctor: %s", report)
            except Exception as e:  # noqa: BLE001 - doctor failures stay visible and retry next minute
                log.exception("flow doctor failed: %s", e)
                ctx.events.emit("flow_doctor.error", outcome="failed", reason=type(e).__name__)
        stop.wait(2)
