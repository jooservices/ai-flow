"""Human review metrics derived from Jira transitions, runs, and fenced write audits."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from statistics import median
from typing import Any

REASONS = ("question_from_ai", "decision_needed", "review_before_merge", "blocked")
SUGGESTIONS = {
    "question_from_ai": "Add the missing product or technical detail to the ticket before development starts.",
    "decision_needed": "Move recurring owner decisions into project policy and acceptance criteria.",
    "review_before_merge": "Reduce the require-human gate to changes that need a release or risk decision.",
    "blocked": "Resolve repeated dependencies or make their owners and expected dates explicit.",
}
WAITING = {"Human review", "Needs owner"}


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _reason(status: str, ticket: dict, latest_run: dict | None) -> str:
    if status == "Human review":
        return "review_before_merge"
    if status == "Needs owner":
        if ticket.get("_has_open_blocker", bool(ticket.get("blocked_by"))):
            return "blocked"
        if "owner-escalated" in (ticket.get("labels") or []):
            return "decision_needed"
        if latest_run and latest_run.get("step") == "dev" and latest_run.get("outcome") == "blocked":
            return "question_from_ai"
        return "decision_needed"
    if ticket.get("_has_open_blocker", bool(ticket.get("blocked_by"))):
        return "blocked"
    return "blocked"


def _week_start(value: datetime) -> datetime:
    utc = value.astimezone(timezone.utc)
    return datetime.combine(utc.date() - timedelta(days=utc.weekday()), time.min, tzinfo=timezone.utc)


def calculate_review_metrics(tickets: list[dict], events: list[dict], runs: list[dict], audits: list[dict],
                             now: datetime | None = None) -> dict:
    """Return a stable set of counts and percentages; all periods use UTC calendar weeks."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    this_week = _week_start(now)
    weeks = [this_week - timedelta(weeks=offset) for offset in range(8, -1, -1)]
    week_counts = {week: {reason: 0 for reason in REASONS} for week in weeks}
    by_key: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        by_key[str(run.get("ticket_key") or "")].append(run)
    for values in by_key.values():
        values.sort(key=lambda row: _dt(row.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc))

    ticket_by_key = {str(ticket.get("key")): dict(ticket) for ticket in tickets}
    for ticket in ticket_by_key.values():
        blockers = ticket.get("blocked_by") or []
        ticket["_has_open_blocker"] = any(
            str(blocker) not in ticket_by_key
            or str(ticket_by_key[str(blocker)].get("status") or "") not in {"Done", "Closed"}
            for blocker in blockers
        )
    transitions: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    transition_times: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    ai_handoffs: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    def add_transition(key: str, at: datetime, status: str) -> bool:
        # The control API records a transition in both write_audit and events. Fold those paired writes
        # into one state change so wait durations and weekly counts do not double.
        if any(previous_status == status and abs((at - previous_at).total_seconds()) <= 10
               for previous_at, previous_status in transition_times[key]):
            return False
        transition_times[key].append((at, status))
        transitions[key].append((at, status))
        return True

    for row in events:
        if row.get("type") != "jira.transition":
            continue
        at = _dt(row.get("ts"))
        key = str(row.get("ticket") or "")
        status = str(row.get("outcome") or (row.get("data") or {}).get("to") or "")
        if not at or not key:
            continue
        is_ai_question_handoff = row.get("reason") == "reviewer asked the AI"
        if is_ai_question_handoff:
            ai_handoffs[key].append((at, status))
            continue
        if add_transition(key, at, status) and status in WAITING:
            ticket = ticket_by_key.get(key, {})
            prior = [r for r in by_key.get(key, []) if (_dt(r.get("started_at")) or now) <= at]
            latest = next((r for r in reversed(prior) if r.get("step") == "dev"), None)
            reason = _reason(status, ticket, latest)
            week = _week_start(at)
            if week in week_counts:
                week_counts[week][reason] += 1
    for row in audits:
        if row.get("target") != "jira" or row.get("action") != "transition" or not row.get("accepted"):
            continue
        at = _dt(row.get("ts"))
        key = str(row.get("ticket") or "")
        status = str((row.get("payload") or {}).get("to") or "")
        if not at or not key:
            continue
        if any(previous_status == status and abs((at - previous_at).total_seconds()) <= 10
               for previous_at, previous_status in ai_handoffs[key]):
            continue
        if not add_transition(key, at, status) or status not in WAITING:
            continue
        ticket = ticket_by_key.get(key, {})
        prior = [r for r in by_key.get(key, []) if (_dt(r.get("started_at")) or now) <= at]
        week = _week_start(at)
        if week in week_counts:
            latest = next((r for r in reversed(prior) if r.get("step") == "dev"), None)
            week_counts[week][_reason(status, ticket, latest)] += 1
    for key, ticket in ticket_by_key.items():
        status = str(ticket.get("status") or "")
        if status not in WAITING and not ticket.get("_has_open_blocker"):
            continue
        history = transitions.get(key, [])
        history.sort()
        latest_status = history[-1][1] if history else ""
        if history and latest_status == status and status in WAITING:
            # The current waiting period was already counted at its transition.
            continue
        if not history and status in WAITING:
            at = _dt(ticket.get("jira_updated") or ticket.get("synced_at"))
            if at:
                week = _week_start(at)
                if week in week_counts:
                    latest = next((r for r in reversed(by_key.get(key, [])) if r.get("step") == "dev"), None)
                    week_counts[week][_reason(status, ticket, latest)] += 1
        elif ticket.get("_has_open_blocker") and status not in WAITING:
            at = _dt(ticket.get("jira_updated") or ticket.get("synced_at"))
            if at:
                week = _week_start(at)
                if week in week_counts:
                    week_counts[week]["blocked"] += 1

    audit_by_key: dict[str, list[dict]] = defaultdict(list)
    for row in audits:
        if row.get("accepted"):
            audit_by_key[str(row.get("ticket") or "")].append(row)
    for values in audit_by_key.values():
        values.sort(key=lambda row: _dt(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))

    # Completed ticket cohort: tickets finished during the previous eight UTC calendar weeks.
    cohort_start = this_week - timedelta(weeks=8)
    finished = []
    needs_by_key: dict[str, bool] = defaultdict(bool)
    for key, rows in transitions.items():
        if any(status in WAITING for _, status in rows):
            needs_by_key[key] = True
    for key, rows in audit_by_key.items():
        if any(str(row.get("action")) in {"approve", "rework", "reject", "skip-gate", "answer", "ask-ai"}
               for row in rows):
            needs_by_key[key] = True
    for key, rows in by_key.items():
        if any(row.get("account_id") == "reviewer-console" and row.get("step") == "qa" for row in rows):
            needs_by_key[key] = True
    for ticket in tickets:
        if "require-human" in (ticket.get("labels") or []):
            needs_by_key[str(ticket.get("key"))] = True
        if ticket.get("status") != "Done":
            continue
        done_at = _dt(ticket.get("done_at") or ticket.get("jira_updated") or ticket.get("synced_at"))
        if done_at and cohort_start <= done_at <= now:
            finished.append(str(ticket.get("key")))
    no_person = sum(not needs_by_key[key] for key in finished)

    waits: list[float] = []
    for key, rows in transitions.items():
        rows.sort()
        opened: datetime | None = None
        for at, status in rows:
            if status in WAITING:
                opened = at
            elif opened:
                if opened >= cohort_start:
                    waits.append(max(0.0, (at - opened).total_seconds() / 60))
                opened = None
        ticket = ticket_by_key.get(key)
        if opened and opened >= cohort_start and ticket and ticket.get("status") in WAITING:
            waits.append(max(0.0, (now - opened).total_seconds() / 60))
    for key, ticket in ticket_by_key.items():
        if ticket.get("status") in WAITING and not transitions.get(key):
            opened = _dt(ticket.get("jira_updated") or ticket.get("synced_at"))
            if opened and opened >= cohort_start:
                waits.append(max(0.0, (now - opened).total_seconds() / 60))

    decided: list[list[dict]] = []
    for key, rows in audit_by_key.items():
        choices = [row for row in rows if str(row.get("action")) in {"approve", "rework", "reject"}]
        if not choices:
            legacy = {"merged": "approve", "rework": "rework", "rejected": "reject"}
            choices = [{"action": legacy[row["outcome"]], "ts": row.get("started_at")}
                       for row in by_key.get(key, []) if row.get("account_id") == "reviewer-console"
                       and row.get("step") == "qa" and row.get("outcome") in legacy]
        if choices:
            decided.append(choices)
    for key, rows in by_key.items():
        if key in audit_by_key:
            continue
        legacy = {"merged": "approve", "rework": "rework", "rejected": "reject"}
        choices = [{"action": legacy[row["outcome"]], "ts": row.get("started_at")}
                   for row in rows if row.get("account_id") == "reviewer-console" and row.get("step") == "qa"
                   and row.get("outcome") in legacy]
        if choices:
            decided.append(choices)
    approved_first = sum(str(rows[0].get("action")) == "approve" for rows in decided)
    sent_back = sum(any(str(row.get("action")) in {"rework", "reject"} for row in rows) for rows in decided)
    decision_count = len(decided)

    weekly = []
    for week in weeks:
        counts = week_counts[week]
        weekly.append({"week": week.date().isoformat(), "by_reason": counts, "total": sum(counts.values())})
    current = week_counts[this_week]
    eight_weeks = week_counts[this_week - timedelta(weeks=8)]
    return {
        "people_needed_this_week": sum(current.values()),
        "people_needed_eight_weeks_ago": sum(eight_weeks.values()),
        "finished_with_no_person_pct": round(100 * no_person / len(finished), 1) if finished else None,
        "finished_tickets": len(finished),
        "median_wait_minutes": round(median(waits), 1) if waits else None,
        "approved_first_time_pct": round(100 * approved_first / decision_count, 1) if decision_count else None,
        "sent_back_or_rejected_pct": round(100 * sent_back / decision_count, 1) if decision_count else None,
        "decided_tickets": decision_count,
        "weekly": weekly,
        "suggestions": [{"reason": reason, "suggestion": SUGGESTIONS[reason]} for reason in REASONS],
    }
