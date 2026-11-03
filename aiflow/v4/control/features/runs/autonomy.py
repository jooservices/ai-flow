"""The weekly autonomy report: what finished without a person, what needed one, and what failed repeatedly."""
from datetime import datetime, timedelta, timezone
from typing import Any

from ... import escalations
from ... import quota as quota_reporting
from ...features.work.constants import utcnow


def trend_index(first_monday, weeks: int, value: datetime) -> int | None:
    date = value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    offset = (date - first_monday).days // 7
    return offset if 0 <= offset < weeks else None


def empty_trend(first_monday, weeks: int) -> list[dict[str, Any]]:
    return [{"week_start": (first_monday + timedelta(days=index * 7)).isoformat(), "finished_without_person": 0,
             "needed_person": 0, "repeated_failures": 0} for index in range(weeks)]


def finished_without_person(ctx, scope, start, end) -> list[dict]:
    scope_params: tuple = (list(scope),) if scope is not None else ()
    return ctx.db.all(
        "SELECT DISTINCT t.key, t.summary, t.done_at FROM tickets t WHERE t.status='Done'"
        + (" AND t.project=ANY(%s)" if scope is not None else "") +
        " AND t.done_at >= %s AND t.done_at <= %s AND EXISTS (SELECT 1 FROM runs r"
        " WHERE r.ticket_key=t.key AND r.status<>'running' AND lower(r.outcome) IN ('done','pass','passed')"
        " AND r.started_at <= t.done_at AND NOT EXISTS (SELECT 1 FROM runs later"
        " WHERE later.ticket_key=t.key AND later.started_at > r.started_at AND later.started_at <= t.done_at))"
        " AND NOT EXISTS (SELECT 1 FROM write_audit w WHERE w.ticket=t.key AND w.action='transition'"
        " AND w.payload->>'source'='human-review' AND w.result='Done' AND w.ts <= t.done_at)", scope_params + (start, end))


def needed_a_person(ctx, scope, start, end) -> list[dict]:
    # A person limited to some projects gets the report of those projects only.
    ticket_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))" if scope is not None else ""
    scope_params: tuple = (list(scope),) if scope is not None else ()
    return ctx.db.all("SELECT observation_key, ticket, reason, options, sent_at FROM master_escalations"
                      " WHERE sent_at >= %s AND sent_at <= %s" + ticket_filter + " ORDER BY sent_at DESC",
                      (start, end) + scope_params)


def repeated_failure_events(ctx, scope, start, end) -> list[dict]:
    ticket_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))" if scope is not None else ""
    scope_params: tuple = (list(scope),) if scope is not None else ()
    return ctx.db.all("SELECT ts, type FROM events WHERE type IN ('flow_bug.created','flow_bug.repeated')"
                      " AND ts >= %s AND ts <= %s" + ticket_filter, (start, end) + scope_params)


def current_repeated_failures(ctx, scope, week_start) -> list[dict]:
    scope_params: tuple = (list(scope),) if scope is not None else ()
    return ctx.db.all(
        "SELECT f.ticket_key, f.signature, f.repeat_count, f.updated_at, t.summary, t.status"
        " FROM flow_bug_tracker f LEFT JOIN tickets t ON t.key=f.ticket_key"
        " WHERE f.updated_at >= %s" + (" AND t.project=ANY(%s)" if scope is not None else "") +
        " ORDER BY f.updated_at DESC LIMIT 50", (week_start,) + scope_params)


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
