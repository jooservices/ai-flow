"""The weekly autonomy report: what finished without a person, what needed one, and what failed repeatedly."""
from datetime import datetime, timedelta, timezone
from typing import Any

from ... import escalations
from ... import quota as quota_reporting
from ...features.work.constants import utcnow
from aiflow.v4.control.features.tickets import ticket_repository_of


def trend_index(first_monday, weeks: int, value: datetime) -> int | None:
    date = value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    offset = (date - first_monday).days // 7
    return offset if 0 <= offset < weeks else None


def empty_trend(first_monday, weeks: int) -> list[dict[str, Any]]:
    return [{"week_start": (first_monday + timedelta(days=index * 7)).isoformat(), "finished_without_person": 0,
             "needed_person": 0, "repeated_failures": 0} for index in range(weeks)]


def finished_without_person(ctx, scope, start, end) -> list[dict]:
    return ticket_repository_of(ctx).finished_without_person(scope, start, end)


def needed_a_person(ctx, scope, start, end) -> list[dict]:
    return ticket_repository_of(ctx).needed_a_person(scope, start, end)


def repeated_failure_events(ctx, scope, start, end) -> list[dict]:
    return ticket_repository_of(ctx).repeated_failure_events(scope, start, end)


def current_repeated_failures(ctx, scope, week_start) -> list[dict]:
    return ticket_repository_of(ctx).current_repeated_failures(scope, week_start)


def count_into_trend(trend: list[dict], first_monday, weeks: int, rows: list[dict], date_key: str, field: str) -> None:
    for item in rows:
        i = trend_index(first_monday, weeks, item[date_key])
        if i is not None:
            trend[i][field] += 1


def autonomy_report(ctx, weeks: int = 8, scope: tuple[str, ...] | None = None) -> dict[str, Any]:
    now = utcnow()
    this_monday = now.date() - timedelta(days=now.weekday())
    first_monday = this_monday - timedelta(days=(weeks - 1) * 7)
    start = datetime.combine(first_monday, datetime.min.time(), tzinfo=timezone.utc)
    end = now
    trend = empty_trend(first_monday, weeks)
    finished = finished_without_person(ctx, scope, start, end)
    count_into_trend(trend, first_monday, weeks, finished, "done_at", "finished_without_person")
    needed = needed_a_person(ctx, scope, start, end)
    count_into_trend(trend, first_monday, weeks, needed, "sent_at", "needed_person")
    count_into_trend(trend, first_monday, weeks, repeated_failure_events(ctx, scope, start, end), "ts",
                     "repeated_failures")
    week_start = datetime.combine(this_monday, datetime.min.time(), tzinfo=timezone.utc)
    current_finished = [row for row in finished if row["done_at"] >= week_start]
    current_needed = [row for row in needed if row["sent_at"] >= week_start]
    current_repeated = current_repeated_failures(ctx, scope, week_start)
    # Accounts are shared infrastructure, not project data.
    quota_accounts = ctx.db.all("SELECT id,provider,quota,quota_source,quota_observed_at FROM accounts ORDER BY id") \
        if scope is None else []
    quota_state = quota_reporting.assessments(ctx.db, quota_accounts, now)
    return {"week_start": this_monday.isoformat(),
            "finished_without_person": current_finished[:50], "needed_person": current_needed[:50],
            "repeated_failures": current_repeated, "trend": trend,
            "escalations": escalations.weekly_metrics(ctx.db, weeks),
            "quota_accounts": [{"id": a["id"], "provider": a["provider"],
                                "quota_awareness": quota_state.get(a["id"], {})} for a in quota_accounts]}
