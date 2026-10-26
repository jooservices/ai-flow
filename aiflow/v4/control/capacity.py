"""Ticket effort estimates and quota-aware capacity planning from recorded runs.

Estimates use completed development and verification work, grouped by ticket so a run that
needed rework contributes its full token and elapsed-time cost to one historical sample.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.quota import as_utc

MIN_HISTORY = 3
SIZES = ("XS", "S", "M", "L", "XL")
SIZE_BASELINES = {
    "XS": (2_000, 8_000, 5, 30),
    "S": (4_000, 16_000, 15, 90),
    "M": (8_000, 32_000, 30, 180),
    "L": (16_000, 64_000, 60, 360),
    "XL": (32_000, 128_000, 120, 720),
}
WINDOW_HOURS = {"five_hour": 5.0, "primary": 5.0, "seven_day": 168.0, "secondary": 168.0,
                "seven_day_opus": 168.0, "seven_day_sonnet": 168.0, "weekly": 168.0,
                "estimated_week": 168.0}


def _clean(value: Any, fallback: str = "unknown") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text or fallback


def ticket_size(ticket: dict[str, Any]) -> str:
    """Use an explicit size label or Jira estimate first, then a stable text-length scale."""
    labels = [str(label).strip().lower() for label in ticket.get("labels") or []]
    for size in SIZES:
        if f"size-{size.lower()}" in labels or f"size:{size.lower()}" in labels:
            return size
    fields = ticket.get("fields") or {}
    if isinstance(fields, dict):
        points = next((fields[key] for key in ("story_points", "storyPoints", "customfield_10016",
                                                "customfield_10014") if fields.get(key) is not None), None)
        try:
            value = float(points)
        except (TypeError, ValueError):
            value = -1
        if value >= 0:
            return "XS" if value <= 1 else "S" if value <= 3 else "M" if value <= 5 else "L" if value <= 8 else "XL"
    text = f"{ticket.get('summary') or ''}\n{ticket.get('description') or ''}"
    chars = len(text.strip())
    return "XS" if chars <= 600 else "S" if chars <= 1_800 else "M" if chars <= 5_000 else "L" if chars <= 12_000 else "XL"


def ticket_area(ticket: dict[str, Any]) -> str:
    """Prefer an `area-*` label or Jira component; use the project as the consistent fallback."""
    for label in ticket.get("labels") or []:
        label = str(label).strip()
        if label.lower().startswith(("area-", "area:")):
            return _clean(label[5:])
    fields = ticket.get("fields") or {}
    if isinstance(fields, dict):
        components = fields.get("components") or fields.get("component") or []
        if isinstance(components, dict):
            components = [components]
        if isinstance(components, list) and components:
            component = components[0]
            if isinstance(component, dict):
                component = component.get("name") or component.get("key")
            if component:
                return _clean(component)
    return _clean(ticket.get("project"))


def ticket_facts(ticket: dict[str, Any]) -> dict[str, str]:
    return {"type": _clean(ticket.get("type"), "task"), "size": ticket_size(ticket),
            "area": ticket_area(ticket)}


def history(db, project_keys: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """One completed ticket sample, summing its development and verification runs."""
    query = (
        "SELECT t.key,t.project,t.type,t.labels,t.summary,t.description,t.fields,"
        "sum(greatest(0,r.tokens_in)+greatest(0,r.tokens_out))::bigint AS tokens,"
        "sum(greatest(0,extract(epoch FROM r.ended_at-r.started_at)))::float8 AS duration_s,"
        "bool_or(r.outcome='rework') AS reworked "
        "FROM runs r JOIN tickets t ON t.key=r.ticket_key "
        "WHERE r.step IN ('dev','verify') AND r.status='finished' AND r.ended_at IS NOT NULL "
        "AND (%s OR t.project=ANY(%s)) GROUP BY t.key,t.project,t.type,t.labels,t.summary,t.description,t.fields "
        "HAVING count(*) FILTER (WHERE r.tokens_in>0 OR r.tokens_out>0 OR r.ended_at>r.started_at)>0"
    )
    params = (project_keys is None, list(project_keys or ()))
    rows = db.all(query, params) if hasattr(db, "all") else db.execute(query, params).fetchall()
    samples = []
    for row in rows:
        samples.append({**ticket_facts(row), "ticket": row["key"], "tokens": max(0, int(row.get("tokens") or 0)),
                        "duration_s": max(0.0, float(row.get("duration_s") or 0)),
                        "reworked": bool(row.get("reworked"))})
    return samples


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * fraction
    low = int(pos)
    high = min(len(ordered) - 1, low + 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _range(values: list[float], *, floor: float = 0.0) -> dict[str, float]:
    low, high = _percentile(values, .10), _percentile(values, .90)
    # A handful of identical runs must still be presented as a useful range.
    if high - low < max(1.0, abs((low + high) / 2) * .10):
        middle = (low + high) / 2
        low, high = middle * .9, middle * 1.1
    low, high = max(floor, low), max(floor, high)
    # A minimum floor can otherwise flatten widened sub-minute or zero-valued history into one number.
    if round(high, 1) <= round(low, 1):
        high = low + max(.1, abs(low) * .1)
    return {"low": round(low, 1), "high": round(high, 1)}


def _risk_range(samples: list[dict[str, Any]]) -> dict[str, float]:
    count = len(samples)
    if not count:
        return {"low": 0.0, "high": 60.0}
    successes = sum(bool(sample.get("reworked")) for sample in samples)
    probability = successes / count
    z = 1.645  # two-sided 90% Wilson interval
    denom = 1 + z * z / count
    center = (probability + z * z / (2 * count)) / denom
    margin = z * math.sqrt(probability * (1 - probability) / count + z * z / (4 * count * count)) / denom
    return {"low": round(max(0.0, center - margin) * 100, 1),
            "high": round(min(1.0, center + margin) * 100, 1)}


def _cohort(sample: dict[str, Any], facts: dict[str, str], level: int) -> bool:
    if level >= 1 and sample["type"] != facts["type"]:
        return False
    if level >= 2 and sample["size"] != facts["size"]:
        return False
    if level >= 3 and sample["area"] != facts["area"]:
        return False
    return True


def estimate(ticket: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate total ticket tokens, elapsed AI time and rework risk as ranges."""
    facts = ticket_facts(ticket)
    cohorts = [(3, "type, size and area"), (2, "type and size"), (1, "ticket type"), (0, "all ticket types")]
    chosen: list[dict[str, Any]] = []
    basis = "size-based starting range"
    for level, label in cohorts:
        cohort = [sample for sample in samples if _cohort(sample, facts, level)]
        if cohort:
            chosen, basis = cohort, label
            if len(cohort) >= MIN_HISTORY:
                break
    if chosen:
        token_range = _range([float(row["tokens"]) for row in chosen], floor=1.0)
        time_range = _range([float(row["duration_s"]) / 60 for row in chosen], floor=1.0)
        risk = _risk_range(chosen)
        source = "history"
    else:
        token_low, token_high, minute_low, minute_high = SIZE_BASELINES[facts["size"]]
        token_range = {"low": float(token_low), "high": float(token_high)}
        time_range = {"low": float(minute_low), "high": float(minute_high)}
        risk = {"low": 0.0, "high": 60.0}
        source = "size_baseline"
    return {"type": facts["type"], "size": facts["size"], "area": facts["area"],
            "tokens": token_range, "time_minutes": time_range, "rework_risk_pct": risk,
            "samples": len(chosen), "basis": basis, "source": source}


def account_fit(account_id: str, forecast: dict[str, Any], estimate_value: dict[str, Any],
                now: datetime | None = None) -> dict[str, Any]:
    """Check the estimate against every quota balance and every window's exhaustion forecast."""
    now = now or datetime.now(timezone.utc)
    windows = forecast.get("windows") or []
    useful = [window for window in windows if window.get("left_tokens") is not None]
    reliable = forecast.get("source") == "reported" or forecast.get("confidence") in {"medium", "high"}
    upper_tokens = float(estimate_value["tokens"]["high"])
    finishes_at = now + timedelta(minutes=float(estimate_value["time_minutes"]["high"]))
    left_tokens = min((int(window["left_tokens"]) for window in useful), default=None)
    exhaustion = []
    insufficient = []
    for window in windows:
        left = window.get("left_tokens")
        if left is not None and int(left) < upper_tokens:
            insufficient.append(str(window.get("label") or window.get("name") or "quota window"))
        prediction = window.get("prediction") or {}
        exhausts = as_utc(prediction.get("exhaustion_at"))
        if exhausts:
            exhaustion.append(exhausts)
            if exhausts <= finishes_at:
                insufficient.append(f"{window.get('label') or window.get('name') or 'quota window'} forecast to run out during the ticket")
    exhaustion_at = min(exhaustion) if exhaustion else None
    # A near exhaustion prediction is useful evidence of an unsafe account even when that window has no
    # remaining-token balance. Otherwise, missing balances cannot establish that an account is safe.
    imminent_exhaustion = any(item <= finishes_at for item in exhaustion)
    if not reliable:
        reason = "quota history is not reliable enough to forecast this account"
        if imminent_exhaustion:
            reason += "; a quota window predicts exhaustion during the ticket"
        return {"account": account_id, "known": False, "fits": None, "left_tokens": None,
                "exhaustion_at": exhaustion_at.isoformat() if exhaustion_at else None, "reason": reason}
    if not useful and not imminent_exhaustion:
        return {"account": account_id, "known": False, "fits": None, "left_tokens": None,
                "exhaustion_at": exhaustion_at.isoformat() if exhaustion_at else None,
                "reason": "quota history has no remaining-token balance or near-term exhaustion prediction"}
    fits = not insufficient
    if fits:
        reason = f"quota forecast leaves at least {left_tokens:,} tokens and lasts through the estimated work"
    else:
        reason = ", ".join(dict.fromkeys(insufficient))
        if left_tokens is not None:
            reason += f"; only {left_tokens:,} tokens remain"
    return {"account": account_id, "known": True, "fits": fits, "left_tokens": left_tokens,
            "exhaustion_at": exhaustion_at.isoformat() if exhaustion_at else None, "reason": reason}


def choose_account(candidates: list[dict[str, Any]], ticket: dict[str, Any],
                   estimate_value: dict[str, Any], forecasts: dict[str, dict[str, Any]],
                   now: datetime | None = None) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """Return the strongest quota-safe eligible account; retain legacy routing if forecasts are unknown."""
    now = now or datetime.now(timezone.utc)
    if not candidates:
        return None, {}
    fits = {str(row["id"]): account_fit(str(row["id"]), forecasts.get(str(row["id"]), {}), estimate_value, now)
            for row in candidates}
    known = [item for item in fits.values() if item["known"]]
    safe = [row for row in candidates if fits[str(row["id"])]["known"] and fits[str(row["id"])]["fits"]]
    if not known or not safe:
        return None, fits
    size = estimate_value.get("size", ticket_size(ticket))
    labels = {str(label).lower() for label in ticket.get("labels") or []}
    prefer_senior = "tier-senior" in labels or size in {"L", "XL"} or str(ticket.get("priority", "")).lower() == "highest"

    def rank(row: dict[str, Any]) -> tuple[int, int, float, str]:
        account_id = str(row["id"])
        tier = str(row.get("tier") or "middle")
        if prefer_senior:
            strength = {"junior": 0, "middle": 1, "senior": 2}.get(tier, 1)
        else:
            strength = {"junior": 0, "senior": 1, "middle": 2}.get(tier, 2)
        left = float(fits[account_id].get("left_tokens") or 0)
        upper = max(1.0, float(estimate_value["tokens"]["high"]))
        return (1, strength, left / upper, account_id)

    best = sorted(safe, key=rank, reverse=True)[0]
    return str(best["id"]), fits


def weekly_tokens(forecast: dict[str, Any], quota: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Estimate the account's usable quota tokens over the next seven days, respecting its tightest window."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    raw_windows = (quota or {}).get("windows") or {}
    available: list[tuple[int, str]] = []
    for window in forecast.get("windows") or []:
        left = window.get("left_tokens")
        if left is None:
            continue
        name = str(window.get("name") or "")
        raw = raw_windows.get(name) or {}
        try:
            period_h = float(raw.get("window_duration_minutes") or raw.get("window_minutes") or 0) / 60
        except (TypeError, ValueError):
            period_h = 0
        period_h = period_h or WINDOW_HOURS.get(name, 168.0)
        total = max(0, int(left))
        reset = as_utc(window.get("reset_at"))
        quota_limit = window.get("quota_tokens")
        if reset and quota_limit and reset <= horizon:
            replacements = max(1, int((horizon - reset).total_seconds() // (period_h * 3600)) + 1)
            total += max(0, int(quota_limit)) * replacements
        available.append((total, str(window.get("label") or name or "quota")))
    if not available:
        return {"tokens": 0, "window": "unknown", "known": False}
    tokens, window_name = min(available, key=lambda item: item[0])
    return {"tokens": tokens, "window": window_name, "known": True}


def error_report(estimate_value: dict[str, Any], actual_tokens: int, actual_minutes: float,
                 actual_rework: bool) -> dict[str, Any]:
    """Compare a ticket's completed total with its saved estimate."""
    def metric(name: str, actual: float) -> dict[str, Any]:
        bounds = estimate_value[name]
        low, high = float(bounds["low"]), float(bounds["high"])
        midpoint = (low + high) / 2
        delta = actual - midpoint
        return {"actual": round(actual, 1), "range": {"low": low, "high": high},
                "within_range": low <= actual <= high, "difference_from_midpoint": round(delta, 1),
                "error_pct": round((delta / midpoint) * 100, 1) if midpoint else 0.0}
    risk = estimate_value.get("rework_risk_pct") or {"low": 0.0, "high": 100.0}
    return {"tokens": metric("tokens", float(actual_tokens)),
            "time_minutes": metric("time_minutes", float(actual_minutes)),
            "rework": {"predicted_pct": risk, "actual": actual_rework,
                       "within_range": float(risk["low"]) <= (100.0 if actual_rework else 0.0) <= float(risk["high"])} }


def dashboard(db, account_rows: list[dict[str, Any]], forecasts: dict[str, dict[str, Any]],
              project_keys: list[str] | tuple[str, ...] | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """Build the console's next-week quota plan, assignment explanations and completed estimate errors."""
    now = now or datetime.now(timezone.utc)
    project_filter = (project_keys is None, list(project_keys or ()))
    samples = history(db, project_keys)
    ready = db.all(
        "SELECT key,project,type,priority,labels,summary,description,fields FROM tickets "
        "WHERE status='To Do' AND 'aiflow-ready'=ANY(labels) AND NOT ('aiflow-hold'=ANY(labels)) "
        "AND (%s OR project=ANY(%s)) ORDER BY priority,key", project_filter)
    ready_estimates = [estimate(ticket, samples) for ticket in ready]
    if ready_estimates:
        average_tokens = sum(item["tokens"]["high"] for item in ready_estimates) / len(ready_estimates)
    else:
        average_tokens = estimate({"type": "Task", "project": "", "summary": "", "labels": ["size-m"]},
                                  samples)["tokens"]["high"]
    account_plan = []
    for row in account_rows:
        account_id = str(row["id"])
        forecast = forecasts.get(account_id) or {}
        capacity = weekly_tokens(forecast, row.get("quota") or {}, now)
        tickets = int(capacity["tokens"] // max(1.0, average_tokens)) if capacity["known"] else 0
        account_plan.append({"account": account_id, "provider": str(row.get("provider") or ""),
                             "tokens_available": capacity["tokens"], "tickets": tickets,
                             "limiting_window": capacity["window"], "quota_source": forecast.get("source", "unknown"),
                             "confidence": forecast.get("confidence", "low"),
                             "known": capacity["known"]})
    known_accounts = [row for row in account_plan if row["known"]]
    limiting = min(known_accounts, key=lambda row: (row["tickets"], row["account"])) if known_accounts else None
    total_capacity = sum(row["tickets"] for row in known_accounts)
    decisions_rows = db.all(
        "SELECT DISTINCT ON (r.ticket_key) r.ticket_key,t.project,t.summary,r.account_id,r.provider,r.started_at,"
        "r.details->'capacity'->'estimate' AS estimate,r.details->'capacity'->'assignment_reason' AS reason "
        "FROM runs r JOIN tickets t ON t.key=r.ticket_key "
        "WHERE r.step='dev' AND r.details->'capacity' ? 'estimate' "
        "AND (%s OR t.project=ANY(%s)) ORDER BY r.ticket_key,r.started_at DESC LIMIT 100",
        project_filter)
    decisions = [{"ticket": row["ticket_key"], "project": row["project"], "summary": row["summary"],
                  "account": row["account_id"], "provider": row["provider"],
                  "assigned_at": row["started_at"].isoformat(), "estimate": row.get("estimate") or {},
                  "reason": row.get("reason") or ""} for row in decisions_rows]
    estimate_rows = db.all(
        "SELECT DISTINCT ON (r.ticket_key) r.ticket_key,t.project,t.summary,r.started_at,"
        "r.details->'capacity'->'estimate' AS estimate "
        "FROM runs r JOIN tickets t ON t.key=r.ticket_key "
        "WHERE r.step='dev' AND r.details->'capacity' ? 'estimate' AND t.status IN ('Done','Closed') "
        "AND (%s OR t.project=ANY(%s)) ORDER BY r.ticket_key,r.started_at LIMIT 100",
        project_filter)
    errors = []
    for row in estimate_rows:
        estimate_value = row.get("estimate") or {}
        if not estimate_value:
            continue
        actual = db.one(
            "SELECT coalesce(sum(greatest(0,tokens_in)+greatest(0,tokens_out)),0)::bigint AS tokens,"
            "coalesce(sum(greatest(0,extract(epoch FROM ended_at-started_at))),0)::float8 AS duration_s,"
            "bool_or(outcome='rework') AS reworked FROM runs WHERE ticket_key=%s AND step IN ('dev','verify') "
            "AND status='finished' AND ended_at IS NOT NULL AND started_at >= %s",
            (row["ticket_key"], row["started_at"])) or {}
        actual_tokens = int(actual.get("tokens") or 0)
        actual_minutes = float(actual.get("duration_s") or 0) / 60
        errors.append({"ticket": row["ticket_key"], "project": row["project"], "summary": row["summary"],
                       "estimated_at": row["started_at"].isoformat(), "estimate": estimate_value,
                       "actual": error_report(estimate_value, actual_tokens, actual_minutes,
                                              bool(actual.get("reworked")))})
    return {"week_start": now.date().isoformat(), "ready_tickets": len(ready),
            "estimated_tokens_per_ticket": round(average_tokens), "tickets_finishable": min(len(ready), total_capacity),
            "quota_capacity_tickets": total_capacity,
            "limiting_ai": None if not limiting else {"account": limiting["account"],
                                                        "provider": limiting["provider"],
                                                        "tickets": limiting["tickets"],
                                                        "window": limiting["limiting_window"]},
            "accounts": account_plan, "decisions": decisions, "errors": errors,
            "estimate_method": "P10–P90 of completed ticket history"}
