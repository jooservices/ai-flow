"""Snapshot of the registry for Command Center, Delivery and SA dashboards (one query round, no caching)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import VERSION
from ..core.controlrepo import epic_for_ticket
from ..core.services import SERVICES, endpoint, service_gaps
from ..core.toolchains import missing_tools, toolchain_gaps
from . import role_health, verify_retry
from . import tooling
from .sa import dashboard_data, thresholds
from .shared_accounts import machines_view
from .identity_redaction import redact_account_ids_in_text
from .quota import assessments as quota_assessments
from .repositories.accounts import AccountsRepository
from .repositories.events import EventsRepository
from .repositories.incidents import IncidentsRepository
from .repositories.nodes import NodesRepository
from .repositories.projects import ProjectsRepository
from .repositories.runs import RunsRepository
from .repositories.tickets import TicketsRepository

NODE_OFFLINE_S = 75  # heartbeats every 15-30 s; three missed beats mean the node is gone


def _age(ts: datetime | None) -> int | None:
    return None if ts is None else int((datetime.now(timezone.utc) - ts).total_seconds())


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.isoformat()


def snapshot(ctx, releases: Path | None = None, *, include_signin: bool = False) -> dict[str, Any]:
    from . import release_progress, release_readiness, rollout
    repo = ctx.repo()
    nodes = _nodes(ctx, repo, releases)
    node_status = {node["name"]: node["status"] for node in nodes}
    runs_per_account, running, active, recent = _runs(ctx)
    accounts = _accounts(ctx, repo, node_status, runs_per_account, running, include_signin)
    tickets, slow_verifications = _tickets(ctx, repo, active, recent)
    result = _dashboard_summary(ctx, repo, nodes, accounts, tickets)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "env": ctx.settings.env,
        "control_repo": repo.ref, "versions": repo.versions, "version": VERSION,
        "links": {"o2": "/o2/", "mail": "/mail/", "gatus": ctx.settings.gatus_url},
        "tool_waits": tooling.waiting_summary(ctx),
        "nodes": nodes, "accounts": accounts, "active_runs": active, "recent_runs": recent,
        "projects": result["projects"], "tickets": tickets,
        "slow_verifications": slow_verifications,
        "release_readiness": release_readiness.all_versions(ctx.db, repo.projects),
        "release_progress": release_progress.all_versions(ctx.db, repo.projects),
        "incidents": result["incidents"], "incidents_recent": result["incidents_recent"],
        "events": result["events"], "sa": dashboard_data(ctx, nodes, accounts),
        "role_health": role_health.summary(ctx), "rollout": rollout.summary(ctx.db),
        "version_policy": {"control": ctx.settings.control_version or VERSION,
                           "max_minor_lag": ctx.settings.version_max_minor_lag},
        "totals": result["totals"],
    }


def scope_snapshot(ctx, data: dict, visible_projects: list[str] | None, reveal_accounts: bool,
                   redact_machine_data: bool) -> dict:
    """Apply the Dashboard's project and machine-identity permissions to a fresh snapshot."""
    if not reveal_accounts:
        redact_account_ids_in_text(data)
    verify_retry.scope_dashboard(data, visible_projects, reveal_accounts)
    if visible_projects is not None:
        _scope_projects(ctx, data, set(visible_projects))
    if redact_machine_data:
        _redact_machine_data(data)
    return data


def _scope_projects(ctx, data: dict, visible: set[str]) -> None:
    data["projects"] = [project for project in data["projects"] if project["key"] in visible]
    data["tickets"] = [ticket for ticket in data["tickets"] if ticket["project"] in visible]
    ticket_keys = {ticket["key"] for ticket in data["tickets"]}
    _scope_project_activity(data, ticket_keys, visible)
    _scope_project_rollups(ctx, data, visible)
    _scope_project_resources(data, visible)
    data["sa"] = {}
    data["version_policy"] = {}


def _scope_project_activity(data: dict, ticket_keys: set[str], visible: set[str]) -> None:
    data["active_runs"] = [run for run in data["active_runs"] if run.get("project") in visible]
    data["recent_runs"] = [run for run in data["recent_runs"] if run.get("ticket") in ticket_keys]
    data["events"] = [event for event in data["events"] if event.get("ticket") in ticket_keys]


def _scope_project_rollups(ctx, data: dict, visible: set[str]) -> None:
    # Project-level artifacts carry different shapes across releases; omit aggregates for restricted users.
    data["release_readiness"] = []
    data["release_progress"] = []
    data["rollout"] = {}
    data["totals"] = _visible_totals(ctx, visible)


def _visible_totals(ctx, visible: set[str]) -> dict:
    totals = RunsRepository(ctx.db).dashboard_totals(sorted(visible))
    integer_fields = {"runs_24h", "tokens_in_24h", "tokens_out_24h", "done_24h", "failed_24h"}
    return {key: int(value) if key in integer_fields else float(value) for key, value in totals.items()}


def _scope_project_resources(data: dict, visible: set[str]) -> None:
    visible_nodes = {node["name"] for node in data["nodes"] if visible.intersection(node.get("projects") or [])}
    visible_tickets = {ticket["key"] for ticket in data["tickets"]}
    data["nodes"] = [node for node in data["nodes"] if node["name"] in visible_nodes]
    data["accounts"] = [account for account in data["accounts"] if _account_has_visible_node(account, visible_nodes)]
    data["incidents"] = _visible_incidents(data["incidents"], visible, visible_nodes, visible_tickets)
    data["incidents_recent"] = _visible_incidents(data["incidents_recent"], visible, visible_nodes, visible_tickets)


def _account_has_visible_node(account: dict, visible_nodes: set[str]) -> bool:
    return any(machine.get("node") in visible_nodes for machine in account.get("machines") or [])


def _visible_incidents(incidents: list[dict], visible: set[str], visible_nodes: set[str],
                       visible_tickets: set[str]) -> list[dict]:
    return [item for item in incidents if item.get("subject") in visible | visible_nodes | visible_tickets]


def _redact_machine_data(data: dict) -> None:
    data["nodes"] = []
    data["accounts"] = []
    data["sa"] = {}
    data["role_health"] = {"roles": [], "settings": {}}
    data["version_policy"] = {}
    data["incidents"] = [item for item in data["incidents"] if item.get("kind") == verify_retry.KIND]
    data["incidents_recent"] = [item for item in data["incidents_recent"]
                                if item.get("kind") == verify_retry.KIND]
    data["events"] = [_pick(event, ("ts", "type", "ticket", "outcome", "reason")) for event in data["events"]]
    data["active_runs"] = [_pick(run, ("id", "ticket", "project", "summary", "step", "elapsed_s",
                                        "heartbeat_age_s", "lease_ms", "progress")) for run in data["active_runs"]]
    data["recent_runs"] = [_pick(run, ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                        "duration_s", "pr_url", "already_done", "ended_age_s"))
                           for run in data["recent_runs"]]
    for ticket in data["tickets"]:
        _redact_ticket_runs(ticket)


def _pick(data: dict, keys: tuple[str, ...]) -> dict:
    return {key: data.get(key) for key in keys}


def _redact_ticket_runs(ticket: dict) -> None:
    if ticket.get("run"):
        ticket["run"] = _pick(ticket["run"], ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress"))
    if ticket.get("last_run"):
        ticket["last_run"] = _pick(ticket["last_run"], ("id", "ticket", "summary", "step", "outcome",
                                                           "error_kind", "duration_s", "pr_url", "already_done",
                                                           "ended_age_s"))


def _nodes(ctx, repo, releases: Path | None) -> list[dict]:
    assigned = _assigned_accounts(repo)
    active_by_node = RunsRepository(ctx.db).dashboard_busy_by_node()
    rows = NodesRepository(ctx.db).dashboard_nodes()
    return [_node_entry(ctx, repo, row, assigned, active_by_node, releases) for row in rows]


def _assigned_accounts(repo) -> dict[str, int]:
    assigned: dict[str, int] = {}
    for account in repo.accounts:
        for machine in account.machines:
            assigned[machine] = assigned.get(machine, 0) + 1
    return assigned


def _node_entry(ctx, repo, row: dict, assigned: dict[str, int], active_by_node: dict[str, int],
                releases: Path | None) -> dict:
    age = _age(row["last_heartbeat"])
    status = row["status"]
    if status == "online" and (age is None or age > NODE_OFFLINE_S):
        status = "offline"
    projects = repo.projects_for_node(row["name"], row["projects"], row["dynamic"])
    required = repo.tools_for_node(row["name"], projects)
    configured = repo.nodes.get(row["name"])
    stack_gaps = _node_stack_gaps(repo, configured, row["tools"])
    desired_services = [s for s in (configured.services or []) if isinstance(s, dict)] if configured else []
    node_services = _node_services(repo, projects, row["metrics"])
    return {"name": row["name"], "ip": row["ip"], "services": node_services,
            "role": row["role"], "status": status, "accepting_work": bool(row["accepting_work"]),
            "sessions_busy": active_by_node.get(row["name"], int(row["slots_used"] or 0)),
            "slots_total": row["slots_total"], "slots_used": row["slots_used"], "release": row["release_version"],
            "metrics": row["metrics"], "tools": row["tools"], "has_agent": bool(row["token_hash"]),
            "required_tools": required, "desired_services": _desired_service_rows(desired_services),
            "missing_tools": sorted(set(missing_tools(row["tools"], required)) | stack_gaps),
            "heartbeat_age_s": age, "accounts_assigned": assigned.get(row["name"], 0),
            "in_control_repo": row["name"] in repo.nodes, "last_heartbeat": _iso(row["last_heartbeat"]),
            "idle_since": _iso(row["idle_since"]), "purpose": getattr(configured, "purpose", "") or "",
            "notes": getattr(configured, "notes", "") or "",
            "capabilities": getattr(configured, "capabilities", []) or [], "projects": projects,
            "version_policy": rollout_policy(ctx, row, releases), "joined_at": row["created_at"]}


def _node_stack_gaps(repo, configured, tools: dict) -> set[str]:
    gaps = set()
    for key in configured.projects if configured else []:
        project = repo.projects.get(key)
        if project:
            gaps.update(toolchain_gaps(tools, project.tools, repo.versions, project.stacks,
                                       require_pins=bool(project.stacks)))
    return gaps


def _node_services(repo, projects: list[str], metrics: dict | None) -> list[dict]:
    reported = (metrics or {}).get("services") or {}
    declared = _declared_services(repo, projects)
    gaps = service_gaps(reported, list(declared))
    return [_service_entry(name, declared, reported, gaps)
            for name in sorted(set(declared) | set(reported)) if name in SERVICES]


def _declared_services(repo, projects: list[str]) -> dict[str, list[str]]:
    declared: dict[str, list[str]] = {}
    for key in projects:
        project = repo.projects.get(key)
        for service in project.required_services if project else []:
            declared.setdefault(service, []).append(key)
    return declared


def _service_entry(name: str, declared: dict[str, list[str]], reported: dict, gaps: dict) -> dict:
    current = reported.get(name) or {}
    return {"name": name, "projects": declared.get(name, []), "healthy": name not in gaps,
            "state": current.get("state", "unknown"), "endpoint": current.get("endpoint") or endpoint(name),
            "image": current.get("image", ""), "detail": gaps.get(name) or current.get("detail", "")}


def _desired_service_rows(services: list[dict]) -> list[dict]:
    return [{"name": str(service.get("name") or service.get("unit") or "service"),
             "unit": str(service.get("unit") or ""),
             "healthcheck_configured": bool(service.get("healthcheck"))} for service in services]


def rollout_policy(ctx, node: dict, releases: Path | None) -> dict:
    from . import rollout
    return rollout.node_policy(ctx, node["name"], node["release_version"], releases)


def _runs(ctx) -> tuple[dict[str, int], dict[str, dict], list[dict], list[dict]]:
    repository = RunsRepository(ctx.db)
    runs_per_account: dict[str, int] = {}
    running_rows = repository.dashboard_running_rows()
    for row in running_rows:
        runs_per_account[row["account_id"]] = runs_per_account.get(row["account_id"], 0) + 1
    running = {row["account_id"]: row for row in running_rows}
    active = [_active_run(ctx, row) for row in running_rows]
    recent = [_recent_run(row) for row in repository.dashboard_recent_rows()]
    return runs_per_account, running, active, recent


def _active_run(ctx, row: dict) -> dict:
    return {"id": str(row["id"]), "ticket": row["ticket_key"], "project": row["project"],
            "summary": row["summary"], "step": row["step"], "account": row["account_id"],
            "provider": row["provider"], "model": row["model"], "node": row["node"], "fence": row["fence"],
            "elapsed_s": _age(row["started_at"]), "heartbeat_age_s": _age(row["heartbeat_at"]),
            "lease_ms": ctx.leases.ttl_ms(row["ticket_key"]), "progress": row["progress"]}


def _recent_run(row: dict) -> dict:
    duration = int((row["ended_at"] - row["started_at"]).total_seconds()) if row["ended_at"] else None
    return {"id": str(row["id"]), "ticket": row["ticket_key"], "summary": row["summary"], "step": row["step"],
            "account": row["account_id"], "provider": row["provider"], "model": row["model"], "node": row["node"],
            "outcome": row["outcome"] or row["status"], "error_kind": row["error_kind"], "duration_s": duration,
            "cost_usd": float(row["cost_usd"]), "tokens_in": row["tokens_in"], "tokens_out": row["tokens_out"],
            "pr_url": row["pr_url"], "already_done": bool((row["details"] or {}).get("already_done")),
            "ended_age_s": _age(row["ended_at"])}


def _accounts(ctx, repo, node_status: dict[str, str], runs_per_account: dict[str, int],
              running: dict[str, dict], include_signin: bool) -> list[dict]:
    db = ctx.db
    thresholds_value = thresholds(repo)
    accounts_repo = AccountsRepository(db)
    rows = accounts_repo.dashboard_accounts()
    quota_by_account = quota_assessments(db, rows)
    today = RunsRepository(db).dashboard_daily_usage()
    sign_in = accounts_repo.dashboard_sign_in_statuses()
    managed = accounts_repo.dashboard_managed_ids()
    out = []
    for row in rows:
        account = _account_entry(ctx, repo, row, node_status, runs_per_account, running,
                                 quota_by_account, today, sign_in, managed, thresholds_value, include_signin)
        if account:
            out.append(account)
    return out


def _account_entry(ctx, repo, row: dict, node_status: dict[str, str], runs_per_account: dict[str, int],
                   running: dict[str, dict], quota_by_account: dict, today: dict, sign_in: dict,
                   managed: set[str], thresholds_value: dict, include_signin: bool) -> dict | None:
    account = repo.account(row["id"])
    if not account:
        return None
    cooling = ctx.leases.cooling(row["id"])
    active = running.get(row["id"])
    model, effort = repo.model_for(row["provider"], row["tier"])
    return {**_account_identity(ctx, account, row, node_status, sign_in, managed, include_signin),
            **_account_usage(row, quota_by_account, today, thresholds_value),
            **_account_runtime(account, row, cooling, active, node_status, model, effort,
                               runs_per_account),
            "last_error": row["last_error"]}


def _account_identity(ctx, account, row: dict, node_status: dict[str, str], sign_in: dict,
                      managed: set[str], include_signin: bool) -> dict:
    return {"id": row["id"], "provider": row["provider"], "node": _primary_machine(account),
            "tier": row["tier"], "roles": account.roles,
            "machines": machines_view(ctx.db, account, node_status, include_signin=include_signin),
            "session_limit": account.sessions, "shared": len(account.machines) > 1,
            "shared_managed": row["id"] in managed, "explicit_only": bool(account.explicit_only),
            "paused": bool(account.paused), "jobs": _account_jobs(account.roles),
            "sign_in_pending": row["id"] in sign_in, "sign_in_status": sign_in.get(row["id"], "idle")}


def _primary_machine(account) -> str:
    return account.machines[0] if account.machines else ""


def _account_usage(row: dict, quota_by_account: dict, today: dict, thresholds_value: dict) -> dict:
    expiry = row["credential_expires_at"]
    expiry_warning = bool(expiry and expiry <= datetime.now(timezone.utc) + timedelta(
        days=thresholds_value["credential_expiry_days"]))
    windows = (row["quota"] or {}).get("windows") or {}
    used = [window for window in windows.values()
            if isinstance(window, dict) and window.get("utilization") is not None]
    resets = [window.get("resets_at") for window in used if window.get("resets_at")]
    usage = today.get(row["id"], {})
    return {"credential_expires_at": _iso(expiry) or "unknown", "credential_expiry_warning": expiry_warning,
            "quota": row["quota"], "quota_awareness": quota_by_account.get(row["id"], {}),
            "quota_used": max((float(window["utilization"]) for window in used), default=None),
            "quota_resets_at": min(resets) if resets else None,
            "tokens_today": int(usage.get("tokens_in", 0)) + int(usage.get("tokens_out", 0)),
            "cost_today": float(usage.get("cost_usd", 0))}


def _account_runtime(account, row: dict, cooling, active: dict | None, node_status: dict[str, str],
                     model: str, effort: str, runs_per_account: dict[str, int]) -> dict:
    probe = (row["worker_state"] or {}).get("probe") or {}
    return {"status": _account_status(account, row, cooling, active, node_status),
            "provider_status": _provider_status(row, cooling),
            "cooldown": {"reason": cooling[0], "seconds_left": cooling[1]} if cooling else None,
            "cooldown_until": row["cooldown_until"], "cooldown_since": _iso(row["cooldown_since"]),
            "idle_since": _iso(row["idle_since"]),
            "check_failed": probe.get("ok") is False or row["status"] == "error",
            "worker": row["worker_state"], "model": model, "effort": effort,
            "runs_active": runs_per_account.get(row["id"], 0), "parallel": account.sessions,
            "run": _account_run(active)}


def _account_jobs(roles: list[str] | None) -> list[str]:
    return [job for job, role in (("develop", "dev"), ("verify", "verify"),
                                  ("intake", "intake_refiner"), ("master", "master"))
            if role in (roles or [])]


def _account_status(account, row: dict, cooling, running: dict | None, nodes: dict[str, str]) -> str:
    if account.paused:
        return "stopped"
    if row["status"] == "signed_out":
        return "signed_out"
    if cooling:
        return "cooldown"
    if running:
        return "working"
    online = any(nodes.get(machine, "offline") == "online" for machine in account.machines)
    if not online and row["status"] not in ("signed_out", "error", "cooldown"):
        return "node_offline"
    return row["status"]


def _provider_status(row: dict, cooling) -> str:
    if row["status"] in ("signed_out", "error"):
        return row["status"]
    return "cooldown" if cooling else row["status"]


def _account_run(run: dict | None) -> dict | None:
    if not run:
        return None
    return {"id": str(run["id"]), "ticket": run["ticket_key"], "summary": run["summary"], "step": run["step"],
            "elapsed_s": _age(run["started_at"]), "fence": run["fence"], "progress": run["progress"]}


def _tickets(ctx, repo, active: list[dict], recent: list[dict]) -> tuple[list[dict], list[dict]]:
    db = ctx.db
    tickets = TicketsRepository(db).dashboard_tickets()
    by_key = {ticket["key"]: ticket for ticket in tickets}
    for ticket in tickets:
        ticket["epic"] = epic_for_ticket(ticket, by_key)
        project = repo.projects.get(ticket["project"])
        ticket["repository"] = project.repo_for(ticket) if project else ""
    active_by_ticket = {row["ticket"]: row for row in active}
    recent_by_ticket = _latest_runs_by_ticket(recent)
    already_done = _already_done_tickets(db)
    slow = verify_retry.attach_slow_verifications(db, tickets)
    for ticket in tickets:
        ticket["already_done"] = already_done.get(ticket["key"]) if ticket["status"] == "In Verify" else None
        ticket["run"] = active_by_ticket.get(ticket["key"])
        ticket["last_run"] = recent_by_ticket.get(ticket["key"])
        ticket["done_at"] = _iso(ticket["done_at"])
        ticket["updated_age_s"] = _age(ticket.pop("jira_updated"))
    return tickets, slow


def _latest_runs_by_ticket(recent: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for run in recent:
        latest.setdefault(run["ticket"], run)
    return latest


def _already_done_tickets(db) -> dict[str, dict]:
    out = {}
    for row in RunsRepository(db).dashboard_already_done_tickets():
        if (row["details"] or {}).get("already_done"):
            out[row["ticket_key"]] = {"pr_url": row["pr_url"] or "", "run_id": str(row["id"])}
    return out


def _dashboard_summary(ctx, repo, nodes: list[dict], accounts: list[dict], tickets: list[dict]) -> dict:
    counts = _ticket_counts(tickets)
    lifecycle = {row["project"]: row for row in ProjectsRepository(ctx.db).lifecycle_rows()}
    projects = [_project_summary(project, counts.get(project.key, {}), lifecycle.get(project.key, {}))
                for project in repo.projects.values()]
    today = RunsRepository(ctx.db).dashboard_totals()
    incidents = IncidentsRepository(ctx.db)
    return {"projects": projects, "incidents": incidents.dashboard_open(),
            "incidents_recent": incidents.dashboard_recently_closed(),
            "events": EventsRepository(ctx.db).dashboard_recent(),
            "totals": {"runs_24h": today["runs"], "cost_24h": float(today["cost"]),
                       "tokens_in_24h": int(today["tin"]), "tokens_out_24h": int(today["tout"]),
                       "done_24h": today["done"], "failed_24h": today["failed"]}}


def _ticket_counts(tickets: list[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for ticket in tickets:
        counts.setdefault(ticket["project"], {}).setdefault(ticket["status"], 0)
        counts[ticket["project"]][ticket["status"]] += 1
    return counts


def _project_summary(project, counts: dict[str, int], lifecycle: dict) -> dict:
    total = sum(counts.values())
    closed = lifecycle.get("status") == "closed"
    return {"key": project.key, "repo": project.repo, "repositories": project.configured_repositories,
            "base": project.base, "environments": project.environments,
            "work_minutes": project.work_minutes(), "counts": counts,
            "status": "closed" if closed else "open", "closed": closed,
            "closed_at": _iso(lifecycle.get("closed_at")), "closed_by": lifecycle.get("closed_by", ""),
            "report_version": int(lifecycle.get("version", 0)),
            "close_suggested": bool(total and counts.get("Done", 0) == total and not closed)}
