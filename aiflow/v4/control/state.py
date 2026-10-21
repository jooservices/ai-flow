"""Snapshot of the registry for Command Center, Delivery and SA dashboards (one query round, no caching)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .. import VERSION
from ..core.toolchains import missing_tools
from .sa import dashboard_data, thresholds

NODE_OFFLINE_S = 75  # heartbeats every 15-30 s; three missed beats mean the node is gone


def _age(ts: datetime | None) -> int | None:
    return None if ts is None else int((datetime.now(timezone.utc) - ts).total_seconds())


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.isoformat()


def snapshot(ctx) -> dict[str, Any]:
    from . import api as control_api, rollout
    db = ctx.db
    repo = ctx.repo()
    releases = control_api.RELEASES
    sa_thresholds = thresholds(repo)
    nodes = []
    assigned: dict[str, int] = {}
    for a in repo.accounts:
        assigned[a.node] = assigned.get(a.node, 0) + 1
    for n in db.all("SELECT * FROM nodes ORDER BY role DESC, name"):
        age = _age(n["last_heartbeat"])
        status = n["status"]
        if status == "online" and (age is None or age > NODE_OFFLINE_S):
            status = "offline"
        projects = repo.projects_for_node(n["name"], n["projects"], n["dynamic"])
        required = repo.tools_for_node(n["name"], projects)
        configured = repo.nodes.get(n["name"])
        desired_services = [s for s in (configured.services or []) if isinstance(s, dict)] if configured else []
        nodes.append({"name": n["name"], "ip": n["ip"], "role": n["role"], "status": status,
                      "slots_total": n["slots_total"], "slots_used": n["slots_used"], "release": n["release_version"],
                      "metrics": n["metrics"], "tools": n["tools"],
                      "has_agent": bool(n["token_hash"]), "required_tools": required,
                      "desired_services": [{"name": str(s.get("name") or s.get("unit") or "service"),
                                            "unit": str(s.get("unit") or ""),
                                            "healthcheck_configured": bool(s.get("healthcheck"))}
                                           for s in desired_services],
                      "missing_tools": missing_tools(n["tools"], required), "heartbeat_age_s": age,
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
    for a in db.all("SELECT * FROM accounts ORDER BY node, provider, id"):
        cool = ctx.leases.cooling(a["id"])
        r = running.get(a["id"])
        model, effort = repo.model_for(a["provider"], a["tier"])
        acc = repo.account(a["id"])
        expiry = a["credential_expires_at"]
        expiry_warning = bool(expiry and expiry <= datetime.now(timezone.utc) + timedelta(
            days=sa_thresholds["credential_expiry_days"]))
        status = "signed_out" if a["status"] == "signed_out" else ("cooldown" if cool else ("working" if r else (
            "node_offline" if node_status.get(a["node"], "offline") != "online"
            and a["status"] not in ("signed_out", "error", "cooldown") else a["status"])))
        probe = (a["worker_state"] or {}).get("probe") or {}
        accounts.append({
            "id": a["id"], "provider": a["provider"], "node": a["node"], "tier": a["tier"], "roles": a["roles"],
            "explicit_only": bool(acc and acc.explicit_only),
            "status": status, "provider_status": (a["status"] if a["status"] in ("signed_out", "error")
                                                    else "cooldown" if cool else a["status"]),
            "cooldown": {"reason": cool[0], "seconds_left": cool[1]} if cool else None,
            "cooldown_until": a["cooldown_until"], "cooldown_since": _iso(a["cooldown_since"]),
            "idle_since": _iso(a["idle_since"]), "credential_expires_at": _iso(expiry) or "unknown",
            "credential_expiry_warning": expiry_warning, "check_failed": probe.get("ok") is False or a["status"] == "error",
            "quota": a["quota"], "last_error": a["last_error"],
            "worker": a["worker_state"], "model": model, "effort": effort,
            "runs_active": runs_per_account.get(a["id"], 0), "parallel": int(getattr(acc, "parallel", 1) or 1),
            "run": None if not r else {"id": str(r["id"]), "ticket": r["ticket_key"], "summary": r["summary"],
                                        "step": r["step"], "elapsed_s": _age(r["started_at"]), "fence": r["fence"],
                                        "progress": r["progress"]},
        })
    active = []
    for r in db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                    " WHERE r.status='running' ORDER BY r.started_at"):
        active.append({"id": str(r["id"]), "ticket": r["ticket_key"], "project": r["project"], "summary": r["summary"],
                       "step": r["step"], "account": r["account_id"], "provider": r["provider"], "model": r["model"],
                       "node": r["node"], "fence": r["fence"], "elapsed_s": _age(r["started_at"]),
                       "heartbeat_age_s": _age(r["heartbeat_at"]), "lease_ms": ctx.leases.ttl_ms(r["ticket_key"]),
                       "progress": r["progress"]})
    recent = []
    for r in db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                    " WHERE r.status<>'running' ORDER BY r.started_at DESC LIMIT 30"):
        dur = int((r["ended_at"] - r["started_at"]).total_seconds()) if r["ended_at"] else None
        recent.append({"id": str(r["id"]), "ticket": r["ticket_key"], "summary": r["summary"], "step": r["step"],
                       "account": r["account_id"], "provider": r["provider"], "model": r["model"], "node": r["node"],
                       "outcome": r["outcome"] or r["status"], "error_kind": r["error_kind"], "duration_s": dur,
                       "cost_usd": float(r["cost_usd"]), "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                       "pr_url": r["pr_url"], "ended_age_s": _age(r["ended_at"])})
    tickets = db.all("SELECT key, project, status, type, priority, labels, summary, parent_key, blocked_by, jira_updated"
                     " FROM tickets ORDER BY project, key")
    by_status: dict[str, dict[str, int]] = {}
    for t in tickets:
        by_status.setdefault(t["project"], {}).setdefault(t["status"], 0)
        by_status[t["project"]][t["status"]] += 1
    run_by_ticket = {a["ticket"]: a for a in active}
    last_by_ticket: dict[str, dict] = {}
    for r in recent:
        last_by_ticket.setdefault(r["ticket"], r)
    for t in tickets:
        t["run"] = run_by_ticket.get(t["key"])
        t["last_run"] = last_by_ticket.get(t["key"])
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
        projects.append({"key": p.key, "repo": p.repo, "base": p.base, "environments": p.environments,
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
        "nodes": nodes, "accounts": accounts, "active_runs": active, "recent_runs": recent,
        "projects": projects, "tickets": tickets, "incidents": incidents, "incidents_recent": incidents_recent,
        "events": events, "sa": sa, "rollout": rollout.summary(db),
        "version_policy": {"control": ctx.settings.control_version or VERSION,
                           "max_minor_lag": ctx.settings.version_max_minor_lag},
        "totals": {"runs_24h": today["runs"], "cost_24h": float(today["cost"]), "tokens_in_24h": int(today["tin"]),
                   "tokens_out_24h": int(today["tout"]), "done_24h": today["done"], "failed_24h": today["failed"]},
    }
