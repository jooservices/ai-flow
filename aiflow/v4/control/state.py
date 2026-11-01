"""Snapshot of the registry for Command Center, Delivery and SA dashboards (one query round, no caching)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .. import VERSION
from ..core.controlrepo import epic_for_ticket
from ..core.services import SERVICES, endpoint, service_gaps
from ..core.toolchains import missing_tools, toolchain_gaps
from . import flow_doctor, role_health
from . import tooling
from . import load_policy
from .sa import dashboard_data, thresholds
from .shared_accounts import machines_views

NODE_OFFLINE_S = 75  # heartbeats every 15-30 s; three missed beats mean the node is gone


def _age(ts: datetime | None) -> int | None:
    return None if ts is None else int((datetime.now(timezone.utc) - ts).total_seconds())


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.isoformat()


def _active_run_context(db, repo):
    assigned: dict[str, int] = {}
    for account in repo.accounts:
        for machine in account.machines:
            assigned[machine] = assigned.get(machine, 0) + 1
    active_rows = db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                         " WHERE r.status='running' ORDER BY r.started_at")
    active_by_node: dict[str, int] = {}
    runs_per_account: dict[str, int] = {}
    running: dict[str, dict] = {}
    for row in active_rows:
        active_by_node[row["node"]] = active_by_node.get(row["node"], 0) + 1
        runs_per_account[row["account_id"]] = runs_per_account.get(row["account_id"], 0) + 1
        # Keep the most recently started run as the account's representative run.
        running[row["account_id"]] = row
    return assigned, active_rows, active_by_node, runs_per_account, running


def _node_service_views(repo, projects, reported_services):
    declared: dict[str, list[str]] = {}
    for project in projects:
        for service in repo.projects[project].required_services if project in repo.projects else []:
            declared.setdefault(service, []).append(project)
    gaps = service_gaps(reported_services, list(declared))
    return [{"name": svc, "projects": declared.get(svc, []), "healthy": svc not in gaps,
             "state": (reported_services.get(svc) or {}).get("state", "unknown"),
             "endpoint": (reported_services.get(svc) or {}).get("endpoint") or endpoint(svc),
             "image": (reported_services.get(svc) or {}).get("image", ""),
             "detail": gaps.get(svc) or (reported_services.get(svc) or {}).get("detail", "")}
            for svc in sorted(set(declared) | set(reported_services)) if svc in SERVICES]


def _node_view(ctx, repo, releases, sa_thresholds, n, assigned, active_by_node, host_load):
    age = _age(n["last_heartbeat"])
    status = n["status"]
    if status == "online" and (age is None or age > NODE_OFFLINE_S):
        status = "offline"
    projects = repo.projects_for_node(n["name"], n["projects"], n["dynamic"])
    required = repo.tools_for_node(n["name"], projects)
    configured = repo.nodes.get(n["name"])
    inventory_projects = projects if repo.inventory_in_db else configured.projects if configured else []
    stack_gaps = {tool for project in inventory_projects if project in repo.projects
                  for tool in toolchain_gaps(n["tools"], repo.projects[project].tools, repo.versions,
                                             repo.projects[project].stacks,
                                             require_pins=bool(repo.projects[project].stacks))}
    desired_services = ([service for service in (configured.services or []) if isinstance(service, dict)]
                        if configured else [])
    reported_services = (n["metrics"] or {}).get("services") or {}
    hostname = load_policy.host_name(n.get("metrics"))
    host_policy = host_load.get(hostname, {})
    node_services = _node_service_views(repo, projects, reported_services)
    from . import rollout
    return {
        "name": n["name"], "ip": n["ip"], "services": node_services, "role": n["role"], "status": status,
        "accepting_work": bool(n["accepting_work"]),
        "sessions_busy": active_by_node.get(n["name"], int(n["slots_used"] or 0)),
        "slots_total": n["slots_total"], "slots_used": n["slots_used"], "release": n["release_version"],
        "metrics": n["metrics"], "tools": n["tools"],
        "load_limits": load_policy.machine_limits(n.get("load_limits")),
        "load_state": n.get("load_state") or {"mode": "normal", "reason": ""},
        "host_load_limits": load_policy.host_limits(
            {"cpu_load_per_cpu": host_policy["cpu_load_per_cpu"]} if host_policy else None),
        "host_load_state": host_policy.get("state") or {"mode": "normal", "reason": ""},
        "has_agent": bool(n["token_hash"]), "required_tools": required,
        "desired_services": [{"name": str(service.get("name") or service.get("unit") or "service"),
                              "unit": str(service.get("unit") or ""),
                              "healthcheck_configured": bool(service.get("healthcheck"))}
                             for service in desired_services],
        "missing_tools": sorted(set(missing_tools(n["tools"], required)) | stack_gaps),
        "heartbeat_age_s": age, "accounts_assigned": assigned.get(n["name"], 0),
        "in_control_repo": not bool(n["dynamic"]) if repo.inventory_in_db else n["name"] in repo.nodes,
        "last_heartbeat": _iso(n["last_heartbeat"]), "idle_since": _iso(n["idle_since"]),
        "purpose": getattr(repo.nodes.get(n["name"]), "purpose", "") or "",
        "notes": getattr(repo.nodes.get(n["name"]), "notes", "") or "",
        "capabilities": getattr(repo.nodes.get(n["name"]), "capabilities", []) or [],
        "projects": projects,
        "version_policy": rollout.node_policy(ctx, n["name"], n["release_version"], releases),
        "joined_at": n["created_at"],
    }


def _node_section(ctx, db, repo, releases, sa_thresholds):
    assigned, active_rows, active_by_node, runs_per_account, running = _active_run_context(db, repo)
    host_load = {row["hostname"]: row for row in db.all(
        "SELECT hostname,cpu_load_per_cpu,state FROM machine_host_load_limits")}
    nodes = [_node_view(ctx, repo, releases, sa_thresholds, node, assigned, active_by_node, host_load)
             for node in db.all("SELECT * FROM nodes WHERE removed_at IS NULL ORDER BY role DESC, name")]
    node_status = {node["name"]: node["status"] for node in nodes}
    return nodes, node_status, active_rows, runs_per_account, running


def _account_data(ctx, db, repo, node_status, include_signin):
    account_rows = db.all("SELECT * FROM accounts ORDER BY node, provider, id")
    cooldowns = ctx.leases.cooling_many([str(row["id"]) for row in account_rows])
    # Quota forecasts are refreshed by the watchdog; replaying history on each page repeats run/sample scans.
    quota_by_account = {str(row["id"]): row.get("quota_forecast") or {} for row in account_rows}
    account_configs = {str(row["id"]): repo.account(str(row["id"])) for row in account_rows}
    machine_views = machines_views(db, [account for account in account_configs.values() if account], node_status,
                                   include_signin=include_signin)
    today_by_account = {row["account_id"]: row for row in db.all(
        "SELECT account_id,coalesce(sum(tokens_in),0) AS tokens_in,coalesce(sum(tokens_out),0) AS tokens_out,"
        "coalesce(sum(cost_usd),0) AS cost_usd FROM runs WHERE started_at >= date_trunc('day',now()) "
        "GROUP BY account_id")}
    sign_in_states = {row["id"]: row["sign_in_status"] for row in db.all(
        "SELECT id,sign_in_status FROM managed_ai_accounts WHERE removed_at IS NULL AND sign_in_status<>'idle'")}
    account_names = {row["id"]: row["name"] for row in db.all(
        "SELECT id,name FROM managed_ai_accounts WHERE removed_at IS NULL AND name<>''")}
    shared_managed = {row["id"] for row in db.all("SELECT id FROM shared_accounts WHERE managed")}
    return (account_rows, cooldowns, quota_by_account, machine_views, today_by_account,
            sign_in_states, account_names, shared_managed)


def _account_status(row, account, cooldown, running, node_status):
    if account.paused:
        return "stopped"
    if row["status"] == "signed_out":
        return "signed_out"
    if cooldown:
        return "cooldown"
    if running:
        return "working"
    offline = not any(node_status.get(machine, "offline") == "online" for machine in account.machines)
    if offline and row["status"] not in ("signed_out", "error", "cooldown"):
        return "node_offline"
    return row["status"]


def _account_quota(row):
    trusted_quota = (row["quota"] or {}) if row.get("quota_source") else {}
    windows = trusted_quota.get("windows") or {}
    used_windows = [window for window in windows.values()
                    if isinstance(window, dict) and window.get("utilization") is not None]
    resets = [window.get("resets_at") for window in used_windows if window.get("resets_at")]
    return trusted_quota, used_windows, resets


def _account_view(row, repo, sa_thresholds, node_status, running, runs_per_account,
                  cooldowns, quota_by_account, machine_views, today_by_account,
                  sign_in_states, account_names, shared_managed):
    cooldown = cooldowns.get(str(row["id"]))
    run = running.get(row["id"])
    model, effort = repo.model_for(row["provider"], row["tier"])
    account = repo.account(row["id"])
    if not account:
        return None
    expiry = row["credential_expires_at"]
    expiry_warning = bool(expiry and expiry <= datetime.now(timezone.utc) + timedelta(
        days=sa_thresholds["credential_expiry_days"]))
    status = _account_status(row, account, cooldown, run, node_status)
    probe = (row["worker_state"] or {}).get("probe") or {}
    trusted_quota, used_windows, resets = _account_quota(row)
    usage = today_by_account.get(row["id"], {})
    return {
        "id": row["id"], "name": account_names.get(row["id"], ""), "provider": row["provider"],
        "node": account.machines[0] if account.machines else "", "tier": row["tier"], "roles": account.roles,
        "machines": machine_views.get(row["id"], []), "session_limit": account.sessions,
        "shared": len(account.machines) > 1, "shared_managed": row["id"] in shared_managed,
        "explicit_only": bool(account and account.explicit_only), "paused": bool(account.paused),
        "jobs": [job for job, role in (("develop", "dev"), ("verify", "verify"), ("intake", "intake_refiner"),
                                       ("master", "master")) if role in (account.roles or [])],
        "sign_in_pending": row["id"] in sign_in_states,
        "sign_in_status": sign_in_states.get(row["id"], "idle"), "status": status,
        "provider_status": row["status"] if row["status"] in ("signed_out", "error")
        else "cooldown" if cooldown else row["status"],
        "cooldown": {"reason": cooldown[0], "seconds_left": cooldown[1]} if cooldown else None,
        "cooldown_until": row["cooldown_until"], "cooldown_since": _iso(row["cooldown_since"]),
        "idle_since": _iso(row["idle_since"]), "credential_expires_at": _iso(expiry) or "unknown",
        "credential_expiry_warning": expiry_warning,
        "check_failed": probe.get("ok") is False or row["status"] == "error",
        "quota": trusted_quota, "quota_awareness": quota_by_account.get(row["id"], {}),
        "quota_used": max((float(window["utilization"]) for window in used_windows), default=None),
        "quota_resets_at": min(resets) if resets else None,
        "tokens_today": int(usage.get("tokens_in", 0)) + int(usage.get("tokens_out", 0)),
        "cost_today": float(usage.get("cost_usd", 0)), "last_error": row["last_error"],
        "worker": row["worker_state"], "model": model, "effort": effort,
        "runs_active": runs_per_account.get(row["id"], 0), "parallel": account.sessions if account else 1,
        "run": None if not run else {"id": str(run["id"]), "ticket": run["ticket_key"],
                                    "summary": run["summary"], "step": run["step"],
                                    "elapsed_s": _age(run["started_at"]), "fence": run["fence"],
                                    "progress": run["progress"]},
    }


def _account_section(ctx, db, repo, node_status, sa_thresholds, runs_per_account, running, include_signin):
    data = _account_data(ctx, db, repo, node_status, include_signin)
    (account_rows, cooldowns, quota_by_account, machine_views, today_by_account,
     sign_in_states, account_names, shared_managed) = data
    accounts = []
    for row in account_rows:
        account = _account_view(row, repo, sa_thresholds, node_status, running, runs_per_account,
                                cooldowns, quota_by_account, machine_views, today_by_account,
                                sign_in_states, account_names, shared_managed)
        if account:
            accounts.append(account)
    return accounts


def _active_run_view(row, lease_ms):
    return {"id": str(row["id"]), "ticket": row["ticket_key"], "project": row["project"],
            "summary": row["summary"], "step": row["step"], "account": row["account_id"],
            "provider": row["provider"], "model": row["model"], "node": row["node"],
            "fence": row["fence"], "elapsed_s": _age(row["started_at"]),
            "heartbeat_age_s": _age(row["heartbeat_at"]), "lease_ms": lease_ms,
            "progress": row["progress"]}


def _active_runs_section(ctx, active_rows):
    lease_ttls = ctx.leases.ttl_ms_many([row["ticket_key"] for row in active_rows])
    return [_active_run_view(row, lease_ms) for row, lease_ms in zip(active_rows, lease_ttls, strict=False)]


def _recent_run_view(row):
    duration = int((row["ended_at"] - row["started_at"]).total_seconds()) if row["ended_at"] else None
    return {"id": str(row["id"]), "ticket": row["ticket_key"], "summary": row["summary"],
            "step": row["step"], "account": row["account_id"], "provider": row["provider"],
            "model": row["model"], "node": row["node"], "outcome": row["outcome"] or row["status"],
            "error_kind": row["error_kind"], "duration_s": duration, "cost_usd": float(row["cost_usd"]),
            "tokens_in": row["tokens_in"], "tokens_out": row["tokens_out"], "pr_url": row["pr_url"],
            "already_done": bool((row["details"] or {}).get("already_done")), "ended_age_s": _age(row["ended_at"])}


def _recent_runs_section(db):
    rows = db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                  " WHERE r.status<>'running' ORDER BY r.started_at DESC LIMIT 30")
    return [_recent_run_view(row) for row in rows]


def _ticket_base_rows(db, repo):
    tickets = db.all("SELECT key, project, status, type, priority, labels, summary, parent_key, blocked_by, done_at,"
                     " jira_updated FROM tickets ORDER BY project, key")
    ticket_index = {ticket["key"]: ticket for ticket in tickets}
    for ticket in tickets:
        ticket["epic"] = epic_for_ticket(ticket, ticket_index)
        project = repo.projects.get(ticket["project"])
        ticket["repository"] = project.repo_for(ticket) if project else ""
    by_status: dict[str, dict[str, int]] = {}
    for ticket in tickets:
        by_status.setdefault(ticket["project"], {}).setdefault(ticket["status"], 0)
        by_status[ticket["project"]][ticket["status"]] += 1
    return tickets, by_status


def _verified_run_markers(db, tickets):
    # Keep the latest successful developer marker visible after a later verifier run lacks a verdict.
    already_done = {}
    verify_keys = [ticket["key"] for ticket in tickets if ticket["status"] == "In Verify"]
    rows = db.all("SELECT DISTINCT ON (ticket_key) id, ticket_key, pr_url, details FROM runs"
                  " WHERE ticket_key=ANY(%s) AND step='dev' AND outcome='done'"
                  " ORDER BY ticket_key, started_at DESC", (verify_keys,)) if verify_keys else []
    for row in rows:
        if (row["details"] or {}).get("already_done"):
            already_done[row["ticket_key"]] = {"pr_url": row["pr_url"] or "", "run_id": str(row["id"])}
    return already_done


def _ticket_section(db, repo, active, recent):
    tickets, by_status = _ticket_base_rows(db, repo)
    run_by_ticket = {run["ticket"]: run for run in active}
    last_by_ticket: dict[str, dict] = {}
    for run in recent:
        last_by_ticket.setdefault(run["ticket"], run)
    already_done = _verified_run_markers(db, tickets)
    for ticket in tickets:
        ticket["already_done"] = already_done.get(ticket["key"]) if ticket["status"] == "In Verify" else None
        ticket["run"] = run_by_ticket.get(ticket["key"])
        ticket["last_run"] = last_by_ticket.get(ticket["key"])
        ticket["done_at"] = _iso(ticket["done_at"])
        ticket["updated_age_s"] = _age(ticket.pop("jira_updated"))
    return tickets, by_status


def _project_section(repo, by_status, lifecycle):
    projects = []
    for project in repo.projects.values():
        counts = by_status.get(project.key, {})
        total = sum(counts.values())
        current = lifecycle.get(project.key, {})
        closed = current.get("status") == "closed"
        projects.append({"key": project.key, "repo": project.repo, "repositories": project.configured_repositories,
                         "base": project.base, "environments": project.environments,
                         "work_minutes": project.work_minutes(), "counts": counts,
                         "status": "closed" if closed else "open", "closed": closed,
                         "closed_at": _iso(current.get("closed_at")), "closed_by": current.get("closed_by", ""),
                         "report_version": int(current.get("version", 0)),
                         "close_suggested": bool(total and counts.get("Done", 0) == total and not closed)})
    return projects


def _operational_section(db, repo, by_status):
    today = db.one("SELECT count(*) AS runs, coalesce(sum(cost_usd),0) AS cost, coalesce(sum(tokens_in),0) AS tin,"
                   " coalesce(sum(tokens_out),0) AS tout, count(*) FILTER (WHERE outcome='done') AS done,"
                   " count(*) FILTER (WHERE outcome IN ('failed','blocked','timeout','lost')) AS failed"
                   " FROM runs WHERE started_at > now() - interval '24 hours'")
    incidents = db.all("SELECT id, opened_at, severity, kind, subject, title, detail FROM incidents"
                       " WHERE closed_at IS NULL ORDER BY opened_at DESC")
    incidents_recent = db.all("SELECT id, opened_at, closed_at, severity, kind, subject, title, detail FROM incidents"
                              " WHERE closed_at > now() - interval '24 hours' ORDER BY closed_at DESC LIMIT 20")
    events = db.all("SELECT ts, type, ticket, account, node, outcome, reason FROM events"
                    " WHERE type <> 'node.heartbeat' ORDER BY id DESC LIMIT 60")
    lifecycle = {row["project"]: row for row in db.all(
        "SELECT project, status, closed_at, closed_by, version FROM project_lifecycle")}
    projects = _project_section(repo, by_status, lifecycle)
    return today, incidents, incidents_recent, events, projects


def snapshot(ctx, *, include_signin: bool = False) -> dict[str, Any]:
    from . import api as control_api, release_progress, release_readiness, rollout, scale_proof
    db = ctx.db
    repo = ctx.repo()
    releases = control_api.RELEASES
    sa_thresholds = thresholds(repo)
    nodes, node_status, active_rows, runs_per_account, running = _node_section(
        ctx, db, repo, releases, sa_thresholds)
    accounts = _account_section(ctx, db, repo, node_status, sa_thresholds,
                                runs_per_account, running, include_signin)
    active = _active_runs_section(ctx, active_rows)
    recent = _recent_runs_section(db)
    tickets, by_status = _ticket_section(db, repo, active, recent)
    today, incidents, incidents_recent, events, projects = _operational_section(db, repo, by_status)
    sa = dashboard_data(ctx, nodes, accounts)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "env": ctx.settings.env,
        "control_repo": repo.ref, "versions": repo.versions, "version": VERSION,
        "links": {"o2": "/o2/", "mail": "/mail/", "gatus": ctx.settings.gatus_url},
        "flow_doctor": flow_doctor.dashboard(ctx),
        "operator_interventions": flow_doctor.operator_intervention_summary(ctx),
        "tool_waits": tooling.waiting_summary(ctx),
        "nodes": nodes, "accounts": accounts, "active_runs": active, "recent_runs": recent,
        "projects": projects, "tickets": tickets,
        "release_readiness": release_readiness.all_versions(db, repo.projects),
        "release_progress": release_progress.all_versions(db, repo.projects), "scale_proof": scale_proof.summary(db),
        "incidents": incidents, "incidents_recent": incidents_recent,
        "events": events, "sa": sa, "role_health": role_health.summary(ctx), "rollout": rollout.summary(db),
        "version_policy": {"control": ctx.settings.control_version or VERSION,
                           "max_minor_lag": ctx.settings.version_max_minor_lag},
        "totals": {"runs_24h": today["runs"], "cost_24h": float(today["cost"]), "tokens_in_24h": int(today["tin"]),
                   "tokens_out_24h": int(today["tout"]), "done_24h": today["done"], "failed_24h": today["failed"]},
    }
