"""Background loops of the control plane (process `aiflow4 control-loops`).

jira_sync   every 20 s: tickets updated in the last 5 minutes; a full pass every 10 minutes.
watchdog    every 15 s: nodes without heartbeat, runs whose lease is gone, runs past the 30/45/60 minute clocks,
            accounts whose cooldown ended. Opens and closes incidents, alerts people on critical ones.
Each loop runs on one control-plane instance at a time (leader key in Redis).
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone

from .incidents import close_incident, open_incident
from .api import Ctx
from .db import J
from .state import NODE_OFFLINE_S

log = logging.getLogger("aiflow4.loops")
OWNER = f"{socket.gethostname()}:{os.getpid()}"


def jira_sync(ctx: Ctx, full: bool = False) -> int:
    keys = ctx.settings.jira_projects or sorted(ctx.repo().projects)
    if not ctx.jira or not keys:
        return 0
    projects = ",".join(keys)
    jql = f"project in ({projects})" + ("" if full else " AND updated >= -5m")
    n = 0
    for issue in ctx.jira.search(jql + " ORDER BY updated DESC"):
        t = ctx.jira.normalize(issue)
        ctx.db.run(
            "INSERT INTO tickets (key, project, status, type, priority, labels, summary, description, parent_key, blocked_by,"
            " jira_created, jira_updated, synced_at) VALUES (%(key)s,%(project)s,%(status)s,%(type)s,%(priority)s,%(labels)s,"
            " %(summary)s,%(description)s,%(parent_key)s,%(blocked_by)s,%(jira_created)s,%(jira_updated)s, now())"
            " ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, type=EXCLUDED.type, priority=EXCLUDED.priority,"
            " labels=EXCLUDED.labels, summary=EXCLUDED.summary, description=EXCLUDED.description,"
            " parent_key=EXCLUDED.parent_key, blocked_by=EXCLUDED.blocked_by, jira_updated=EXCLUDED.jira_updated, synced_at=now()",
            t)
        n += 1
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
    # nodes that joined but have no AI account: they add slots, never work
    repo = ctx.repo()
    with_accounts = {a.node for a in repo.accounts}
    for n in ctx.db.all("SELECT name, status FROM nodes"):
        if n["status"] == "online" and n["name"] not in with_accounts:
            where = "" if n["name"] in repo.nodes else " and is not described in aiflow-control/nodes/"
            open_incident(ctx, "node_no_ai", n["name"], "warning", f"node {n['name']} has no AI account",
                          f"{n['name']} is online{where}, but no AI account is assigned to it, so it cannot take any ticket."
                          " Assign accounts in aiflow-control/accounts.yaml and sign them in on the node.")
        elif n["name"] in with_accounts or n["status"] != "online":
            close_incident(ctx, "node_no_ai", n["name"], "accounts assigned" if n["name"] in with_accounts else "node left")
    # tickets whose dev runs keep failing (backoff in the API keeps them from spinning; a person should look)
    from .api import BACKOFF_SQL
    streak_sql = BACKOFF_SQL.split("SELECT ticket_key, fails FROM streak")[0] + "SELECT ticket_key, fails FROM streak"
    stuck = {r["ticket_key"]: r["fails"] for r in ctx.db.all(streak_sql)}
    for key, fails in stuck.items():
        if fails >= 3:
            last = ctx.db.one("SELECT account_id, summary FROM runs WHERE ticket_key=%s AND step='dev' ORDER BY started_at DESC"
                              " LIMIT 1", (key,))
            open_incident(ctx, "ticket_stuck", key, "warning", f"{key} failed {fails} runs in a row",
                          f"Last error ({last['account_id'] if last else '?'}): {(last['summary'] if last else '')[:300]}."
                          " Retries wait up to 30 minutes; fix the cause or put the ticket on hold.")
    for i in ctx.db.all("SELECT subject FROM incidents WHERE kind='ticket_stuck' AND closed_at IS NULL"):
        if stuck.get(i["subject"], 0) < 3:
            close_incident(ctx, "ticket_stuck", i["subject"], "a run produced work again")
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
    ready = {r["id"] for r in ctx.db.all("SELECT a.id FROM accounts a JOIN nodes n ON n.name = a.node"
                                         " WHERE a.status IN ('idle','working','probing') AND n.status = 'online'")}
    for role in ("master", "intake_refiner", "verify", "dev"):
        holders = [a for a in repo.accounts if role in (a.roles or [])]
        ok = [a.id for a in holders if a.id in ready]
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
            reset_txt = datetime.fromtimestamp(float(reset), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if reset else "unknown"
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


def run_loops(ctx: Ctx, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    last_sync = last_full = last_wd = 0.0
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
                last_wd = now
        except Exception as e:  # noqa: BLE001
            log.exception("loop error: %s", e)
            ctx.events.emit("control.error", reason=str(e)[:300])
        stop.wait(2)
