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
from .sa import dashboard_data, thresholds
from .shared_accounts import machines_views

NODE_OFFLINE_S = 75  # heartbeats every 15-30 s; three missed beats mean the node is gone


def _age(ts: datetime | None) -> int | None:
    return None if ts is None else int((datetime.now(timezone.utc) - ts).total_seconds())


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.isoformat()


def snapshot(ctx, *, include_signin: bool = False) -> dict[str, Any]:
    from . import api as control_api, release_progress, release_readiness, rollout, scale_proof
    db = ctx.db
    repo = ctx.repo()
    releases = control_api.RELEASES
    sa_thresholds = thresholds(repo)
    nodes = []
    assigned: dict[str, int] = {}
    for a in repo.accounts:
        for machine in a.machines:
            assigned[machine] = assigned.get(machine, 0) + 1
    active_by_node = {row["node"]: int(row["busy"]) for row in db.all(
        "SELECT node,count(*) AS busy FROM runs WHERE status='running' GROUP BY node")}
    for n in db.all("SELECT * FROM nodes WHERE removed_at IS NULL ORDER BY role DESC, name"):
        age = _age(n["last_heartbeat"])
        status = n["status"]
        if status == "online" and (age is None or age > NODE_OFFLINE_S):
            status = "offline"
        projects = repo.projects_for_node(n["name"], n["projects"], n["dynamic"])
        required = repo.tools_for_node(n["name"], projects)
        configured = repo.nodes.get(n["name"])
        stack_gaps = {tool for project in (configured.projects if configured else [])
                      if project in repo.projects for tool in toolchain_gaps(
                          n["tools"], repo.projects[project].tools, repo.versions, repo.projects[project].stacks,
                          require_pins=bool(repo.projects[project].stacks))}
        desired_services = [s for s in (configured.services or []) if isinstance(s, dict)] if configured else []
        reported_services = (n["metrics"] or {}).get("services") or {}
        declared: dict[str, list[str]] = {}
        for project in projects:
            for service in repo.projects[project].required_services if project in repo.projects else []:
                declared.setdefault(service, []).append(project)
        gaps = service_gaps(reported_services, list(declared))
        node_services = [{"name": svc, "projects": declared.get(svc, []), "healthy": svc not in gaps,
                          "state": (reported_services.get(svc) or {}).get("state", "unknown"),
                          "endpoint": (reported_services.get(svc) or {}).get("endpoint") or endpoint(svc),
                          "image": (reported_services.get(svc) or {}).get("image", ""),
                          "detail": gaps.get(svc) or (reported_services.get(svc) or {}).get("detail", "")}
                         for svc in sorted(set(declared) | set(reported_services)) if svc in SERVICES]
        nodes.append({"name": n["name"], "ip": n["ip"], "services": node_services,
                      "role": n["role"], "status": status,
                      "accepting_work": bool(n["accepting_work"]),
                      "sessions_busy": active_by_node.get(n["name"], int(n["slots_used"] or 0)),
                      "slots_total": n["slots_total"], "slots_used": n["slots_used"], "release": n["release_version"],
                      "metrics": n["metrics"], "tools": n["tools"],
                      "has_agent": bool(n["token_hash"]), "required_tools": required,
                      "desired_services": [{"name": str(s.get("name") or s.get("unit") or "service"),
                                            "unit": str(s.get("unit") or ""),
                                            "healthcheck_configured": bool(s.get("healthcheck"))}
                                           for s in desired_services],
                      "missing_tools": sorted(set(missing_tools(n["tools"], required)) | stack_gaps),
                      "heartbeat_age_s": age,
                      "accounts_assigned": assigned.get(n["name"], 0), "in_control_repo": n["name"] in repo.nodes,
                      "last_heartbeat": _iso(n["last_heartbeat"]),
                      "idle_since": _iso(n["idle_since"]),
                      "purpose": getattr(repo.nodes.get(n["name"]), "purpose", "") or "",
                      "notes": getattr(repo.nodes.get(n["name"]), "notes", "") or "",
                      "capabilities": getattr(repo.nodes.get(n["name"]), "capabilities", []) or [],
                      "projects": projects,
                      "version_policy": rollout.node_policy(ctx, n["name"], n["release_version"], releases),
                      "joined_at": n["created_at"]})
    node_status = {n["name"]: n["status"] for n in nodes}
    runs_per_account: dict[str, int] = {}
    for r in db.all("SELECT account_id FROM runs WHERE status='running'"):
        runs_per_account[r["account_id"]] = runs_per_account.get(r["account_id"], 0) + 1
    running = {r["account_id"]: r for r in db.all(
        "SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key WHERE r.status='running'")}
    accounts = []
    account_rows = db.all("SELECT * FROM accounts ORDER BY node, provider, id")
    cooldowns = ctx.leases.cooling_many([str(row["id"]) for row in account_rows])
    # The watchdog refreshes account quota forecasts. `/v1/state` is loaded by every console
    # view, so replaying history here would repeat the same run/sample scans per page request.
    quota_by_account = {str(row["id"]): row.get("quota_forecast") or {} for row in account_rows}
    account_configs = {str(row["id"]): repo.account(str(row["id"])) for row in account_rows}
    machine_views = machines_views(db, [acc for acc in account_configs.values() if acc], node_status,
                                   include_signin=include_signin)
    today_by_account = {row["account_id"]: row for row in db.all(
        "SELECT account_id,coalesce(sum(tokens_in),0) AS tokens_in,coalesce(sum(tokens_out),0) AS tokens_out,"
        "coalesce(sum(cost_usd),0) AS cost_usd FROM runs WHERE started_at >= date_trunc('day',now()) "
        "GROUP BY account_id")}
    sign_in_states = {row["id"]: row["sign_in_status"] for row in db.all(
        "SELECT id,sign_in_status FROM managed_ai_accounts WHERE removed_at IS NULL AND sign_in_status<>'idle'")}
    shared_managed = {row["id"] for row in db.all("SELECT id FROM shared_accounts WHERE managed")}
    for a in account_rows:
        cool = cooldowns.get(str(a["id"]))
        r = running.get(a["id"])
        model, effort = repo.model_for(a["provider"], a["tier"])
        acc = repo.account(a["id"])
        if not acc:
            continue
        expiry = a["credential_expires_at"]
        expiry_warning = bool(expiry and expiry <= datetime.now(timezone.utc) + timedelta(
            days=sa_thresholds["credential_expiry_days"]))
        status = "stopped" if acc.paused else ("signed_out" if a["status"] == "signed_out" else ("cooldown" if cool else ("working" if r else (
            "node_offline" if not any(node_status.get(machine, "offline") == "online" for machine in acc.machines)
            and a["status"] not in ("signed_out", "error", "cooldown") else a["status"]))))
        probe = (a["worker_state"] or {}).get("probe") or {}
        windows = (a["quota"] or {}).get("windows") or {}
        used_windows = [window for window in windows.values()
                        if isinstance(window, dict) and window.get("utilization") is not None]
        resets = [window.get("resets_at") for window in used_windows if window.get("resets_at")]
        usage = today_by_account.get(a["id"], {})
        accounts.append({
            "id": a["id"], "provider": a["provider"], "node": (acc.machines[0] if acc.machines else ""),
            "tier": a["tier"], "roles": acc.roles,
            "machines": machine_views.get(a["id"], []),
            "session_limit": acc.sessions, "shared": len(acc.machines) > 1,
            "shared_managed": a["id"] in shared_managed,
            "explicit_only": bool(acc and acc.explicit_only),
            "paused": bool(acc.paused), "jobs": [job for job, role in
                (("develop", "dev"), ("verify", "verify"), ("intake", "intake_refiner"), ("master", "master"))
                if role in (acc.roles or [])],
            "sign_in_pending": a["id"] in sign_in_states,
            "sign_in_status": sign_in_states.get(a["id"], "idle"),
            "status": status, "provider_status": (a["status"] if a["status"] in ("signed_out", "error")
                                                    else "cooldown" if cool else a["status"]),
            "cooldown": {"reason": cool[0], "seconds_left": cool[1]} if cool else None,
            "cooldown_until": a["cooldown_until"], "cooldown_since": _iso(a["cooldown_since"]),
            "idle_since": _iso(a["idle_since"]), "credential_expires_at": _iso(expiry) or "unknown",
            "credential_expiry_warning": expiry_warning, "check_failed": probe.get("ok") is False or a["status"] == "error",
            "quota": a["quota"], "quota_awareness": quota_by_account.get(a["id"], {}),
            "quota_used": max((float(w["utilization"]) for w in used_windows), default=None),
            "quota_resets_at": min(resets) if resets else None,
            "tokens_today": int(usage.get("tokens_in", 0)) + int(usage.get("tokens_out", 0)),
            "cost_today": float(usage.get("cost_usd", 0)), "last_error": a["last_error"],
            "worker": a["worker_state"], "model": model, "effort": effort,
            "runs_active": runs_per_account.get(a["id"], 0), "parallel": acc.sessions if acc else 1,
            "run": None if not r else {"id": str(r["id"]), "ticket": r["ticket_key"], "summary": r["summary"],
                                        "step": r["step"], "elapsed_s": _age(r["started_at"]), "fence": r["fence"],
                                        "progress": r["progress"]},
        })
    active = []
    active_rows = db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                         " WHERE r.status='running' ORDER BY r.started_at")
    lease_ttls = ctx.leases.ttl_ms_many([r["ticket_key"] for r in active_rows])
    for r, lease_ms in zip(active_rows, lease_ttls):
        active.append({"id": str(r["id"]), "ticket": r["ticket_key"], "project": r["project"], "summary": r["summary"],
                       "step": r["step"], "account": r["account_id"], "provider": r["provider"], "model": r["model"],
                       "node": r["node"], "fence": r["fence"], "elapsed_s": _age(r["started_at"]),
                       "heartbeat_age_s": _age(r["heartbeat_at"]), "lease_ms": lease_ms,
                       "progress": r["progress"]})
    recent = []
    for r in db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                    " WHERE r.status<>'running' ORDER BY r.started_at DESC LIMIT 30"):
        dur = int((r["ended_at"] - r["started_at"]).total_seconds()) if r["ended_at"] else None
        recent.append({"id": str(r["id"]), "ticket": r["ticket_key"], "summary": r["summary"], "step": r["step"],
                       "account": r["account_id"], "provider": r["provider"], "model": r["model"], "node": r["node"],
                       "outcome": r["outcome"] or r["status"], "error_kind": r["error_kind"], "duration_s": dur,
                       "cost_usd": float(r["cost_usd"]), "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                       "pr_url": r["pr_url"], "already_done": bool((r["details"] or {}).get("already_done")),
                       "ended_age_s": _age(r["ended_at"])})
    tickets = db.all("SELECT key, project, status, type, priority, labels, summary, parent_key, blocked_by, done_at, jira_updated"
                     " FROM tickets ORDER BY project, key")
    ticket_index = {ticket["key"]: ticket for ticket in tickets}
    for ticket in tickets:
        ticket["epic"] = epic_for_ticket(ticket, ticket_index)
        project = repo.projects.get(ticket["project"])
        ticket["repository"] = project.repo_for(ticket) if project else ""
    by_status: dict[str, dict[str, int]] = {}
    for t in tickets:
        by_status.setdefault(t["project"], {}).setdefault(t["status"], 0)
        by_status[t["project"]][t["status"]] += 1
    run_by_ticket = {a["ticket"]: a for a in active}
    last_by_ticket: dict[str, dict] = {}
    for r in recent:
        last_by_ticket.setdefault(r["ticket"], r)
    # The marker comes from the ticket's latest successful developer run, so a later verifier run that ends
    # without a verdict (quota, timeout) does not hide it while the ticket stays in verification.
    already_done = {}
    verify_keys = [ticket["key"] for ticket in tickets if ticket["status"] == "In Verify"]
    for r in db.all("SELECT DISTINCT ON (ticket_key) id, ticket_key, pr_url, details FROM runs"
                    " WHERE ticket_key=ANY(%s) AND step='dev' AND outcome='done'"
                    " ORDER BY ticket_key, started_at DESC", (verify_keys,)) if verify_keys else []:
        if (r["details"] or {}).get("already_done"):
            already_done[r["ticket_key"]] = {"pr_url": r["pr_url"] or "", "run_id": str(r["id"])}
    for t in tickets:
        t["already_done"] = already_done.get(t["key"]) if t["status"] == "In Verify" else None
        t["run"] = run_by_ticket.get(t["key"])
        t["last_run"] = last_by_ticket.get(t["key"])
        t["done_at"] = _iso(t["done_at"])
        t["updated_age_s"] = _age(t.pop("jira_updated"))
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
    projects = []
    for p in repo.projects.values():
        counts = by_status.get(p.key, {})
        total = sum(counts.values())
        current = lifecycle.get(p.key, {})
        closed = current.get("status") == "closed"
        projects.append({"key": p.key, "repo": p.repo, "repositories": p.configured_repositories,
                         "base": p.base, "environments": p.environments,
                         "work_minutes": p.work_minutes(), "counts": counts,
                         "status": "closed" if closed else "open", "closed": closed,
                         "closed_at": _iso(current.get("closed_at")), "closed_by": current.get("closed_by", ""),
                         "report_version": int(current.get("version", 0)),
                         "close_suggested": bool(total and counts.get("Done", 0) == total and not closed)})
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
