"""Quota windows, usage rates and run-out forecasts derived from recorded provider and run history."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, pstdev
from typing import Any


UTC = timezone.utc
WINDOW_HOURS = {"five_hour": 5.0, "primary": 5.0, "seven_day": 168.0, "secondary": 168.0,
                "seven_day_opus": 168.0, "seven_day_sonnet": 168.0, "weekly": 168.0,
                "estimated_week": 168.0}


def as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            return None
    return None


def _ratio(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number / 100 if number > 1 else number))


def _run_usage(run: dict) -> tuple[int, float, int]:
    tokens = max(0, int(run.get("tokens_in") or 0)) + max(0, int(run.get("tokens_out") or 0))
    try:
        cost = max(0.0, float(run.get("cost_usd") or 0))
    except (TypeError, ValueError):
        cost = 0.0
    return tokens, cost, max(0, int(run.get("tokens_in") or 0))


def _run_time(run: dict) -> datetime | None:
    return as_utc(run.get("started_at"))


def _run_duration(run: dict) -> float:
    try:
        value = float(run.get("duration_s") or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    start, end = as_utc(run.get("started_at")), as_utc(run.get("ended_at"))
    return max(0.0, (end - start).total_seconds()) if start and end else 0.0


def _confidence(runs: list[dict], now: datetime) -> tuple[str, int, float | None]:
    recent = [r for r in runs if (started := _run_time(r)) and now - timedelta(hours=24) <= started <= now]
    measured = [(r, _run_usage(r)[0] / duration) for r in recent
                if (duration := _run_duration(r)) > 0]
    ranked = sorted(measured, key=lambda pair: _run_usage(pair[0])[2])
    trim = int(len(ranked) * .05)
    steady_rates = [rate * 3600 for _, rate in ranked[:len(ranked) - trim or None]]
    measured_count = len(measured)
    if measured_count < 8 or not steady_rates:
        return "low", measured_count, None
    center = mean(steady_rates)
    spread = pstdev(steady_rates) if len(steady_rates) > 1 else 0.0
    ratio = spread / center if center else None
    if measured_count >= 20 and ratio is not None and ratio < .5:
        return "high", measured_count, ratio
    if measured_count >= 8 and ratio is not None and ratio < .5:
        return "medium", measured_count, ratio
    return "low", measured_count, ratio


def _trimmed(runs: list[dict]) -> list[dict]:
    ordered = sorted(runs, key=lambda r: _run_usage(r)[2])
    remove = int(len(ordered) * .05)
    return ordered[:len(ordered) - remove] if remove else ordered


def _rates(runs: list[dict], now: datetime, start: datetime) -> dict[str, float]:
    used = [r for r in runs if (at := _run_time(r)) and start <= at <= now]
    steady = _trimmed(used)
    hours = max((now - start).total_seconds() / 3600, 1 / 60)
    tokens = sum(_run_usage(r)[0] for r in steady)
    cost = sum(_run_usage(r)[1] for r in steady)
    return {"tokens_per_hour": round(tokens / hours, 2), "cost_usd_per_hour": round(cost / hours, 8),
            "runs": len(used), "steady_runs": len(steady)}


def _period_hours(name: str, value: dict) -> float:
    try:
        minutes = float(value.get("window_duration_minutes") or value.get("window_minutes") or 0)
        if minutes > 0:
            return minutes / 60
    except (TypeError, ValueError):
        pass
    return WINDOW_HOURS.get(name, 168.0)


def _sample_rate(samples: list[dict], name: str) -> float | None:
    points = []
    for sample in samples:
        quota = sample.get("quota") or {}
        windows = quota.get("windows") or {}
        window = windows.get(name)
        if not isinstance(window, dict):
            continue
        at = as_utc(sample.get("sampled_at"))
        utilization = _ratio(window.get("utilization", window.get("used_percent")))
        if at and utilization is not None:
            points.append((at, utilization))
    points.sort()
    for (before, old), (after, current) in reversed(list(zip(points, points[1:]))):
        hours = (after - before).total_seconds() / 3600
        if hours > 0 and current >= old:
            return (current - old) / hours
    return None


def _learned_reset(name: str, samples: list[dict], runs: list[dict], now: datetime,
                   period_h: float) -> tuple[datetime | None, str]:
    resets: list[datetime] = []
    points = []
    for sample in samples:
        quota = sample.get("quota") or {}
        window = ((quota.get("windows") or {}).get(name) or {})
        reset = as_utc(window.get("resets_at") or window.get("reset_at"))
        if reset:
            resets.append(reset)
        at = as_utc(sample.get("sampled_at"))
        utilization = _ratio(window.get("utilization", window.get("used_percent")))
        if at and utilization is not None:
            points.append((at, utilization))
    valid = [reset for reset in resets if reset > now]
    if valid:
        return min(valid), "reported"
    points.sort()
    for (before_at, before), (after_at, after) in reversed(list(zip(points, points[1:]))):
        if before - after >= .35:
            learned = after_at + timedelta(hours=period_h)
            if learned > now:
                return learned, "learned"
    for run in runs:
        reset = as_utc(run.get("reset_at"))
        if reset and reset > now and str(run.get("outcome") or "") == "quota":
            return reset, "learned"
    return None, "unknown"


def _history(runs: list[dict], now: datetime) -> list[dict]:
    first = now.date() - timedelta(days=6)
    days: dict[date, dict] = {first + timedelta(days=i): {"day": (first + timedelta(days=i)).isoformat(),
                                                          "tokens_in": 0, "tokens_out": 0, "tokens": 0,
                                                          "cost_usd": 0.0, "runs": 0}
                             for i in range(7)}
    for run in runs:
        at = _run_time(run)
        if not at or at.date() not in days:
            continue
        row = days[at.date()]
        tokens, cost, tokens_in = _run_usage(run)
        row["tokens_in"] += tokens_in
        row["tokens_out"] += max(0, int(run.get("tokens_out") or 0))
        row["tokens"] += tokens
        row["cost_usd"] += cost
        row["runs"] += 1
    for row in days.values():
        row["cost_usd"] = round(row["cost_usd"], 6)
    return list(days.values())


def _estimate_capacity(runs: list[dict], window_start: datetime, now: datetime) -> int:
    """Estimate a weekly budget at 125% of the busiest prior complete UTC week; bootstrap from observed use."""
    week_start = window_start.date()
    totals: dict[date, int] = defaultdict(int)
    for run in runs:
        at = _run_time(run)
        if at and at.date() < week_start and (at.date() + timedelta(days=7)) <= now.date():
            monday = at.date() - timedelta(days=at.weekday())
            if monday + timedelta(days=7) <= week_start:
                totals[monday] += _run_usage(run)[0]
    observed = max(totals.values(), default=0)
    current = sum(_run_usage(r)[0] for r in runs
                  if (at := _run_time(r)) and window_start <= at <= now)
    basis = observed or current or 1000
    return max(1000, int(math.ceil(basis * 1.25)))


def _prediction(used_fraction: float | None, capacity: float | None, tokens_used: int,
                tokens_per_hour: float, fraction_per_hour: float | None, reset: datetime | None,
                now: datetime, confidence: str) -> dict:
    remaining_fraction = None if used_fraction is None else max(0.0, 1 - used_fraction)
    remaining_units = (max(0.0, capacity * remaining_fraction) if capacity is not None and remaining_fraction is not None
                       else max(0.0, capacity - tokens_used) if capacity is not None else None)
    unit_rate = tokens_per_hour if capacity is not None else None
    rate_fraction = fraction_per_hour
    if rate_fraction is None and unit_rate and capacity:
        rate_fraction = unit_rate / capacity
    if capacity is None and used_fraction is not None and tokens_used > 0 and used_fraction > 0:
        token_capacity = tokens_used / used_fraction
        remaining_units = token_capacity * (1 - used_fraction)
        unit_rate = tokens_per_hour
    remaining_at_reset = None
    exhaustion_at = None
    state = "unknown"
    before_reset = False
    if reset and reset > now:
        hours_left = (reset - now).total_seconds() / 3600
        if capacity is not None and unit_rate is not None:
            remaining_at_reset = (remaining_units - unit_rate * hours_left) / capacity
            if unit_rate > 0 and remaining_units is not None:
                exhaustion_at = now + timedelta(hours=remaining_units / unit_rate)
                before_reset = exhaustion_at < reset
        elif remaining_fraction is not None and rate_fraction is not None:
            remaining_at_reset = remaining_fraction - rate_fraction * hours_left
            if rate_fraction > 0:
                exhaustion_at = now + timedelta(hours=remaining_fraction / rate_fraction)
                before_reset = exhaustion_at < reset
        if before_reset or (remaining_at_reset is not None and remaining_at_reset < 0):
            state = "will run out before reset"
        elif remaining_at_reset is not None and remaining_at_reset > .2:
            state = "comfortable"
        elif remaining_at_reset is not None:
            state = "tight"
        elif remaining_fraction is not None:
            state = "comfortable" if remaining_fraction > .2 else "tight"
    return {"status": state, "before_reset": before_reset, "confidence": confidence,
            "exhaustion_at": exhaustion_at.isoformat() if exhaustion_at else None,
            "remaining_at_reset_pct": None if remaining_at_reset is None else round(max(0, remaining_at_reset) * 100, 1),
            "remaining_now_pct": None if remaining_fraction is None else round(remaining_fraction * 100, 1)}


def account_awareness(provider: str, quota: dict | None, runs: list[dict], samples: list[dict],
                     now: datetime | None = None) -> dict:
    """Build the account's seven-day usage, burn rates, reset windows and exhaustion forecasts."""
    now = as_utc(now) or datetime.now(UTC)
    quota = quota or {}
    runs = sorted((r for r in runs if _run_time(r) and _run_time(r) <= now), key=lambda r: _run_time(r))
    samples = sorted(samples, key=lambda s: as_utc(s.get("sampled_at")) or datetime.min.replace(tzinfo=UTC))
    confidence, run_count, spread = _confidence(runs, now)
    daily_start = now - timedelta(hours=24)
    hour_start = now - timedelta(hours=1)
    weekly_start = now - timedelta(days=7)
    base_rates = {"last_hour": _rates(runs, now, hour_start), "last_day": _rates(runs, now, daily_start),
                  "whole_window": _rates(runs, now, weekly_start)}
    window_reports = quota.get("windows") if isinstance(quota.get("windows"), dict) else {}
    estimate = not bool(window_reports)
    if estimate:
        monday = now.date() - timedelta(days=now.weekday())
        window_start = datetime.combine(monday, time.min, UTC)
        reset = window_start + timedelta(days=7)
        reset_source = "estimate"
        learned_reset, _ = _learned_reset("estimated_week", samples, runs, now, 168)
        if learned_reset:
            reset = learned_reset
            window_start = reset - timedelta(days=7)
            reset_source = "learned"
        capacity = _estimate_capacity(runs, window_start, now)
        usage_runs = [r for r in runs if (at := _run_time(r)) and window_start <= at <= now]
        used_tokens = sum(_run_usage(r)[0] for r in usage_runs)
        rate = _rates(runs, now, window_start)
        used_fraction = min(1.0, used_tokens / capacity)
        pred = _prediction(used_fraction, float(capacity), used_tokens, rate["tokens_per_hour"], None,
                           reset, now, confidence)
        window_reports = {"estimated_week": {"utilization": used_fraction, "resets_at": reset.isoformat(),
                                              "estimated_limit_tokens": capacity}}
        windows = [{"name": "estimated_week", "label": "Estimated weekly", "source": "estimate",
                    "used_pct": round(used_fraction * 100, 1), "left_pct": round(max(0, 1-used_fraction) * 100, 1),
                    "used_tokens": used_tokens, "left_tokens": max(0, capacity-used_tokens),
                    "quota_tokens": capacity, "reset_at": reset.isoformat(), "reset_source": reset_source,
                    "tokens": sum(_run_usage(r)[0] for r in usage_runs),
                    "cost_usd": round(sum(_run_usage(r)[1] for r in usage_runs), 6),
                    "burn_rates": {"last_hour": base_rates["last_hour"], "last_day": base_rates["last_day"],
                                   "whole_window": rate}, "prediction": pred}]
    else:
        windows = []
        for name, value in sorted(window_reports.items()):
            if not isinstance(value, dict):
                continue
            utilization = _ratio(value.get("utilization", value.get("used_percent")))
            period_h = _period_hours(name, value)
            reset, reset_source = _learned_reset(name, samples, runs, now, period_h)
            supplied_reset = as_utc(value.get("resets_at") or value.get("reset_at"))
            if supplied_reset and supplied_reset > now:
                reset, reset_source = supplied_reset, "reported"
            if reset:
                window_start = reset - timedelta(hours=period_h)
            else:
                window_start = now - timedelta(hours=period_h)
            usage_runs = [r for r in runs if (at := _run_time(r)) and window_start <= at <= now]
            used_tokens = sum(_run_usage(r)[0] for r in usage_runs)
            used_cost = sum(_run_usage(r)[1] for r in usage_runs)
            rates = {"last_hour": _rates(runs, now, max(hour_start, window_start)),
                     "last_day": _rates(runs, now, max(daily_start, window_start)),
                     "whole_window": _rates(runs, now, window_start)}
            try:
                capacity = float(value.get("quota_tokens") or value.get("limit_tokens") or value.get("total_tokens") or 0)
            except (TypeError, ValueError):
                capacity = 0
            capacity = capacity or None
            fraction_rate = _sample_rate(samples, name)
            if fraction_rate is None and utilization is not None and reset and reset > now:
                elapsed_h = max(.01, period_h - (reset - now).total_seconds() / 3600)
                if elapsed_h >= .25:
                    fraction_rate = utilization / elapsed_h
            effective_capacity = capacity
            if effective_capacity is None and utilization and used_tokens:
                effective_capacity = used_tokens / utilization
            pred = _prediction(utilization, effective_capacity, used_tokens,
                               rates["whole_window"]["tokens_per_hour"], fraction_rate, reset, now, confidence)
            left_pct = None if utilization is None else round(max(0, 1-utilization) * 100, 1)
            left_tokens = None if effective_capacity is None or utilization is None else max(
                0, int(effective_capacity * (1-utilization)))
            windows.append({"name": name, "label": name.replace("_", " ").title(),
                            "source": "reported", "used_pct": None if utilization is None else round(utilization*100, 1),
                            "left_pct": left_pct, "used_tokens": used_tokens, "left_tokens": left_tokens,
                            "quota_tokens": capacity, "reset_at": reset.isoformat() if reset else None,
                            "reset_source": reset_source, "tokens": used_tokens,
                            "cost_usd": round(used_cost, 6), "burn_rates": rates, "prediction": pred})
    largest = max((r for r in runs if (at := _run_time(r)) and at >= weekly_start),
                  key=lambda r: _run_usage(r)[2], default=None)
    largest_run = None
    if largest:
        token_count, cost, context_tokens = _run_usage(largest)
        largest_run = {"ticket": largest.get("ticket_key") or largest.get("ticket"),
                       "model": largest.get("model") or "", "tokens": token_count, "context_tokens": context_tokens,
                       "cost_usd": round(cost, 6),
                       "duration_s": round(_run_duration(largest), 1)}
    usage_runs = [r for r in runs if _run_time(r) and _run_time(r) >= weekly_start]
    return {"source": "estimate" if estimate else "reported", "is_estimate": estimate,
            "runs_24h": run_count, "confidence": confidence, "rate_spread_ratio": None if spread is None else round(spread, 3),
            "burn_rates": base_rates,
            "usage_7d": {"tokens_in": sum(_run_usage(r)[2] for r in usage_runs),
                         "tokens_out": sum(max(0, int(r.get("tokens_out") or 0)) for r in usage_runs),
                         "tokens": sum(_run_usage(r)[0] for r in usage_runs),
                         "cost_usd": round(sum(_run_usage(r)[1] for r in usage_runs), 6),
                         "runs": len(usage_runs)},
            "largest_run": largest_run, "history": _history(runs, now), "windows": windows,
            "at_risk": any(w["prediction"]["status"] == "will run out before reset" for w in windows)}
