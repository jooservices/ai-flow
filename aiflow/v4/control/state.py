"""Snapshot of the whole system for Command Center and Delivery view (one query round, no caching)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

NODE_OFFLINE_S = 75  # heartbeats every 15-30 s; three missed beats mean the node is gone


def _age(ts: datetime | None) -> int | None:
    return None if ts is None else int((datetime.now(timezone.utc) - ts).total_seconds())


def snapshot(ctx) -> dict[str, Any]:
    db = ctx.db
    repo = ctx.repo()
    nodes = []
    for n in db.all("SELECT * FROM nodes ORDER BY role DESC, name"):
        age = _age(n["last_heartbeat"])
        status = n["status"]
        if status == "online" and (age is None or age > NODE_OFFLINE_S):
            status = "offline"
        nodes.append({"name": n["name"], "ip": n["ip"], "role": n["role"], "status": status,
                      "slots_total": n["slots_total"], "slots_used": n["slots_used"], "release": n["release_version"],
                      "metrics": n["metrics"], "tools": n["tools"], "heartbeat_age_s": age})
    running = {r["account_id"]: r for r in db.all(
        "SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key WHERE r.status='running'")}
    accounts = []
    for a in db.all("SELECT * FROM accounts ORDER BY node, provider, id"):
        cool = ctx.leases.cooling(a["id"])
        r = running.get(a["id"])
        model, effort = repo.model_for(a["provider"], a["tier"])
        acc = repo.account(a["id"])
        accounts.append({
            "id": a["id"], "provider": a["provider"], "node": a["node"], "tier": a["tier"], "roles": a["roles"],
            "explicit_only": bool(acc and acc.explicit_only),
            "status": "cooldown" if cool else ("working" if r else a["status"]),
            "cooldown": {"reason": cool[0], "seconds_left": cool[1]} if cool else None,
            "cooldown_until": a["cooldown_until"], "quota": a["quota"], "last_error": a["last_error"],
            "worker": a["worker_state"], "model": model, "effort": effort,
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
    events = db.all("SELECT ts, type, ticket, account, node, outcome, reason FROM events ORDER BY id DESC LIMIT 60")
    projects = [{"key": p.key, "repo": p.repo, "base": p.base, "environments": p.environments,
                 "work_minutes": p.work_minutes(), "counts": by_status.get(p.key, {})} for p in repo.projects.values()]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "env": ctx.settings.env,
        "control_repo": repo.ref, "versions": repo.versions,
        "nodes": nodes, "accounts": accounts, "active_runs": active, "recent_runs": recent,
        "projects": projects, "tickets": tickets, "incidents": incidents, "events": events,
        "totals": {"runs_24h": today["runs"], "cost_24h": float(today["cost"]), "tokens_in_24h": int(today["tin"]),
                   "tokens_out_24h": int(today["tout"]), "done_24h": today["done"], "failed_24h": today["failed"]},
    }
