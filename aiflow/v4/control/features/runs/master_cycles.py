"""The owner's Dashboard request for a master review, and the master cycle metrics behind the no-action target (JAIF-349)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from ...features.work.constants import utcnow
from ...repositories import master_cycles as cycle_repository

NO_ACTION_TARGET_PERCENT = 5
METRIC_DAYS = 7


def request_review(ctx) -> None:
    cycle_repository.request_review(ctx.db)


def snapshot_with_review_request(ctx, build_snapshot) -> dict:
    """Serve a waiting request in the snapshot built now. A request made later waits for the next cycle."""
    requested = cycle_repository.take_review_request(ctx.db)
    snapshot = build_snapshot()
    snapshot["review_requested"] = requested
    return snapshot


def no_action_percent(ai_calls: int, actions: int) -> float | None:
    """Share of AI calls that completed no action; None while the master has not called the model."""
    if not ai_calls:
        return None
    return round(100 * (ai_calls - actions) / ai_calls, 1)


def cycle_report(ctx, days: int = METRIC_DAYS) -> dict:
    """One row per UTC day, oldest first, ending today; days without cycles still appear."""
    first_day = utcnow().date() - timedelta(days=days - 1)
    since = datetime.combine(first_day, time.min, tzinfo=timezone.utc)
    counts = {row["day"]: row for row in cycle_repository.cycle_counts_by_day(ctx.db, since)}
    daily = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        row = counts.get(day, {})
        ai_calls, actions = int(row.get("ai_calls") or 0), int(row.get("actions") or 0)
        daily.append({"day": day.isoformat(), "cycles": int(row.get("cycles") or 0), "ai_calls": ai_calls,
                      "actions": actions, "no_action_percent": no_action_percent(ai_calls, actions)})
    return {"days": daily, "target_no_action_percent": NO_ACTION_TARGET_PERCENT}
