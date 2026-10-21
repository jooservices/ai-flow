"""SA dashboard data and capacity recommendations, derived from the control repo and registry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


DEFAULT_THRESHOLDS = {
    "slots_used_percent": 80,
    "slots_duration_minutes": 30,
    "role_cooldown_minutes": 15,
    "idle_days": 7,
    "credential_expiry_days": 7,
}


def thresholds(repo) -> dict[str, int | float]:
    configured = (getattr(repo, "sa", {}) or {}).get("thresholds") or {}
    out: dict[str, int | float] = dict(DEFAULT_THRESHOLDS)
    for key, default in DEFAULT_THRESHOLDS.items():
        try:
            value = float(configured.get(key, default))
            if value > 0:
                out[key] = value if isinstance(default, float) else int(value)
        except (TypeError, ValueError):
            pass
    out["slots_used_percent"] = min(100, out["slots_used_percent"])
    return out


def update_slot_pressure(ctx) -> None:
    """Start or clear the fleet slot-pressure clock after each accepted node heartbeat."""
    limit = thresholds(ctx.repo())["slots_used_percent"]
    totals = ctx.db.one(
        "SELECT coalesce(sum(slots_used),0) AS used, coalesce(sum(slots_total),0) AS total FROM nodes"
        " WHERE status='online' AND last_heartbeat > now() - interval '75 seconds'")
    pressured = totals["total"] > 0 and 100 * totals["used"] / totals["total"] >= limit
    ctx.db.run("UPDATE sa_health SET slot_pressure_since=CASE WHEN %s THEN coalesce(slot_pressure_since,now())"
               " ELSE NULL END WHERE id=true", (pressured,))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _datetime(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def dashboard_data(ctx, nodes: list[dict], accounts: list[dict]) -> dict[str, Any]:
    """Build read-only dashboard sections using only the registry (never calls OpenObserve)."""
    repo = ctx.repo()
    limits = thresholds(repo)
    now = datetime.now(timezone.utc)
    slot_clock = ctx.db.one("SELECT slot_pressure_since FROM sa_health WHERE id=true")
    pressure_since = slot_clock["slot_pressure_since"] if slot_clock else None
    online = [n for n in nodes if n["status"] == "online"]
    slots_total = sum(max(0, int(n["slots_total"] or 0)) for n in online)
    slots_used = sum(max(0, int(n["slots_used"] or 0)) for n in online)
    utilization = round(100 * slots_used / slots_total, 1) if slots_total else None

    recommendations: list[dict[str, str]] = []
    if (utilization is not None and utilization >= limits["slots_used_percent"] and pressure_since and
            pressure_since <= now - timedelta(minutes=limits["slots_duration_minutes"])):
        recommendations.append({"kind": "add_node", "subject": "fleet",
                                "reason": f"Fleet slots have stayed at {utilization:g}% or more for "
                                          f"{limits['slots_duration_minutes']} minutes."})

    account_by_id = {a["id"]: a for a in accounts}
    by_role: dict[str, list[dict]] = {}
    for source in repo.accounts:
        account = account_by_id.get(source.id)
        if account:
            for role in source.roles:
                by_role.setdefault(role, []).append(account)
    for role, members in sorted(by_role.items()):
        threshold_at = now - timedelta(minutes=limits["role_cooldown_minutes"])
        if members and all(a.get("provider_status", a["status"]) == "cooldown" and a.get("cooldown_since") and
                           _datetime(a["cooldown_since"]) and _datetime(a["cooldown_since"]) <= threshold_at
                           for a in members):
            recommendations.append({"kind": "add_account", "subject": role,
                                    "reason": f"Every {role} account has been cooling down for at least "
                                              f"{limits['role_cooldown_minutes']} minutes."})

    idle_at = now - timedelta(days=limits["idle_days"])
    for node in nodes:
        if node["status"] == "online" and _datetime(node.get("idle_since")) and _datetime(node["idle_since"]) <= idle_at:
            recommendations.append({"kind": "remove_node", "subject": node["name"],
                                    "reason": f"Node has had no active slots for {limits['idle_days']} days."})
    for account in accounts:
        if account["status"] == "idle" and _datetime(account.get("idle_since")) and _datetime(account["idle_since"]) <= idle_at:
            recommendations.append({"kind": "remove_account", "subject": account["id"],
                                    "reason": f"Account has been idle for {limits['idle_days']} days."})

    policy_denials = ctx.db.all(
        "SELECT id, ts, ticket, account, node, run_id, reason, data->>'command' AS command FROM events"
        " WHERE type='policy.decision' AND outcome='deny' ORDER BY id DESC LIMIT 30")
    network_events = ctx.db.all(
        "SELECT id, ts, type, ticket, account, node, run_id, outcome, reason,"
        " jsonb_build_object('error_kind',data->'error_kind','retry',data->'retry','attempts',data->'attempts') AS data"
        " FROM events WHERE type='node.heartbeat.error' OR data->>'error_kind'='network'"
        " OR (type LIKE 'network.%' AND data->>'retry'='true')"
        " ORDER BY id DESC LIMIT 50")
    network_runs = ctx.db.all(
        "SELECT id, started_at AS ts, ticket_key AS ticket, project, account_id AS account, node, status,"
        " outcome, error_kind, details->'retry' AS retry, details->>'error_message' AS detail FROM runs"
        " WHERE error_kind='network' OR details->>'error_kind'='network'"
        " ORDER BY started_at DESC LIMIT 50")
    network_issues = [{"source": "event", **event} for event in network_events]
    network_issues.extend({"source": "run", **run} for run in network_runs)
    network_issues.sort(key=lambda issue: issue["ts"], reverse=True)

    provider_blocks = [a for a in accounts if a.get("provider_status", a["status"]) in ("cooldown", "signed_out")]
    return {
        "thresholds": limits,
        "slots": {"used": slots_used, "total": slots_total, "utilization_percent": utilization,
                  "pressure_since": _iso(pressure_since)},
        "recommendations": recommendations,
        "policy_denials": policy_denials,
        "provider_blocks": provider_blocks,
        "network_issues": network_issues[:60],
    }
