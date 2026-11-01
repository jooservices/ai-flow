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


def _group_by_ticket(rows: list[dict], key_field: str, sort_field: str) -> dict[str, list[dict]]:
    """Group rows by their ticket identifier and sort each group chronologically."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key_field) or "")].append(row)
    for values in grouped.values():
        values.sort(key=lambda row: _dt(row.get(sort_field)) or datetime.min.replace(tzinfo=timezone.utc))
    return grouped


def _ticket_index(tickets: list[dict], blocker_statuses: dict[str, str] | None) -> dict[str, dict]:
    """Copy tickets into a key index and attach the existing open-blocker decision."""
    ticket_by_key = {str(ticket.get("key")): dict(ticket) for ticket in tickets}
    known_statuses = blocker_statuses or {}
    for ticket in ticket_by_key.values():
        blockers = ticket.get("blocked_by") or []
        ticket["_has_open_blocker"] = any(
            str(blocker) not in ticket_by_key and known_statuses.get(str(blocker)) not in {"Done", "Closed"}
            or str(blocker) in ticket_by_key
            and str(ticket_by_key[str(blocker)].get("status") or "") not in {"Done", "Closed"}
            for blocker in blockers
        )
    return ticket_by_key


def _add_transition(key: str, at: datetime, status: str,
                    transitions: dict[str, list[tuple[datetime, str]]],
                    transition_times: dict[str, list[tuple[datetime, str]]]) -> bool:
    """Append a state change unless a same-status write was already recorded within ten seconds."""
    if any(previous_status == status and abs((at - previous_at).total_seconds()) <= 10
           for previous_at, previous_status in transition_times[key]):
        return False
    transition_times[key].append((at, status))
    transitions[key].append((at, status))
    return True


def _record_waiting_transition(key: str, at: datetime, status: str, ticket_by_key: dict[str, dict],
                              runs_by_key: dict[str, list[dict]], weeks: list[datetime],
                              week_counts: dict[datetime, dict[str, int]], now: datetime) -> None:
    """Count one waiting transition in its UTC week using the latest earlier development run."""
    if status not in WAITING:
        return
    ticket = ticket_by_key.get(key, {})
    prior = [run for run in runs_by_key.get(key, []) if (_dt(run.get("started_at")) or now) <= at]
    latest = next((run for run in reversed(prior) if run.get("step") == "dev"), None)
    week = _week_start(at)
    if week in week_counts:
        week_counts[week][_reason(status, ticket, latest)] += 1


def _process_events(events: list[dict], ticket_by_key: dict[str, dict], runs_by_key: dict[str, list[dict]],
                    weeks: list[datetime], week_counts: dict[datetime, dict[str, int]], now: datetime
                    ) -> tuple[dict[str, list[tuple[datetime, str]]],
                               dict[str, list[tuple[datetime, str]]],
                               dict[str, list[tuple[datetime, str]]]]:
    """Collect event transitions, AI handoffs, and weekly counts without paired-write duplicates."""
    transitions: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    transition_times: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    ai_handoffs: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for row in events:
        if row.get("type") != "jira.transition":
            continue
        at = _dt(row.get("ts"))
        key = str(row.get("ticket") or "")
        status = str(row.get("outcome") or (row.get("data") or {}).get("to") or "")
        if not at or not key:
            continue
        if row.get("reason") == "reviewer asked the AI":
            ai_handoffs[key].append((at, status))
            continue
        if _add_transition(key, at, status, transitions, transition_times):
            _record_waiting_transition(key, at, status, ticket_by_key, runs_by_key, weeks, week_counts, now)
    return transitions, transition_times, ai_handoffs


def _process_audits(audits: list[dict], ticket_by_key: dict[str, dict], runs_by_key: dict[str, list[dict]],
                    weeks: list[datetime], week_counts: dict[datetime, dict[str, int]], now: datetime,
                    transitions: dict[str, list[tuple[datetime, str]]],
                    transition_times: dict[str, list[tuple[datetime, str]]],
                    ai_handoffs: dict[str, list[tuple[datetime, str]]]) -> None:
    """Add accepted Jira transitions, ignoring event-paired writes and reviewer AI handoffs."""
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
        if _add_transition(key, at, status, transitions, transition_times):
            _record_waiting_transition(key, at, status, ticket_by_key, runs_by_key, weeks, week_counts, now)


def _count_current_waits(ticket_by_key: dict[str, dict], transitions: dict[str, list[tuple[datetime, str]]],
                         runs_by_key: dict[str, list[dict]], weeks: list[datetime],
                         week_counts: dict[datetime, dict[str, int]]) -> None:
    """Count current waiters and unresolved blockers that have no counted current transition."""
    for key, ticket in ticket_by_key.items():
        status = str(ticket.get("status") or "")
        if status not in WAITING and not ticket.get("_has_open_blocker"):
            continue
        history = transitions.get(key, [])
        history.sort()
        latest_status = history[-1][1] if history else ""
        if history and latest_status == status and status in WAITING:
            continue
        if not history and status in WAITING:
            at = _dt(ticket.get("jira_updated") or ticket.get("synced_at"))
            if at:
                week = _week_start(at)
                if week in week_counts:
                    latest = next((run for run in reversed(runs_by_key.get(key, []))
                                   if run.get("step") == "dev"), None)
                    week_counts[week][_reason(status, ticket, latest)] += 1
        elif ticket.get("_has_open_blocker") and status not in WAITING:
            at = _dt(ticket.get("jira_updated") or ticket.get("synced_at"))
            if at:
                week = _week_start(at)
                if week in week_counts:
                    week_counts[week]["blocked"] += 1


def _finished_ticket_summary(tickets: list[dict], transitions: dict[str, list[tuple[datetime, str]]],
                             audits_by_key: dict[str, list[dict]], runs_by_key: dict[str, list[dict]],
                             cohort_start: datetime, now: datetime) -> tuple[int, int]:
    """Return finished cohort size and the number finished without recorded human-review activity."""
    needs_by_key: dict[str, bool] = defaultdict(bool)
    for key, rows in transitions.items():
        if any(status in WAITING for _, status in rows):
            needs_by_key[key] = True
    for key, rows in audits_by_key.items():
        if any(str(row.get("action")) in {"approve", "rework", "reject", "skip-gate", "answer", "ask-ai"}
               for row in rows):
            needs_by_key[key] = True
    for key, rows in runs_by_key.items():
        if any(row.get("account_id") == "reviewer-console" and row.get("step") == "qa" for row in rows):
            needs_by_key[key] = True
    finished = []
    for ticket in tickets:
        key = str(ticket.get("key"))
        if "require-human" in (ticket.get("labels") or []):
            needs_by_key[key] = True
        if ticket.get("status") != "Done":
            continue
        done_at = _dt(ticket.get("done_at") or ticket.get("jira_updated") or ticket.get("synced_at"))
        if done_at and cohort_start <= done_at <= now:
            finished.append(key)
    return len(finished), sum(not needs_by_key[key] for key in finished)


def _wait_durations(transitions: dict[str, list[tuple[datetime, str]]], ticket_by_key: dict[str, dict],
                    cohort_start: datetime, now: datetime) -> list[float]:
    """Measure completed and still-open waiting intervals inside the completed-ticket cohort window."""
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
    return waits


def _decision_summary(audits_by_key: dict[str, list[dict]], runs_by_key: dict[str, list[dict]]) -> tuple[int, int, int]:
    """Summarize first approvals and sent-back decisions, including legacy reviewer QA runs."""
    decided: list[list[dict]] = []
    legacy = {"merged": "approve", "rework": "rework", "rejected": "reject"}
    for key, rows in audits_by_key.items():
        choices = [row for row in rows if str(row.get("action")) in {"approve", "rework", "reject"}]
        if not choices:
            choices = [{"action": legacy[row["outcome"]], "ts": row.get("started_at")}
                       for row in runs_by_key.get(key, []) if row.get("account_id") == "reviewer-console"
                       and row.get("step") == "qa" and row.get("outcome") in legacy]
        if choices:
            decided.append(choices)
    for key, rows in runs_by_key.items():
        if key in audits_by_key:
            continue
        choices = [{"action": legacy[row["outcome"]], "ts": row.get("started_at")}
                   for row in rows if row.get("account_id") == "reviewer-console" and row.get("step") == "qa"
                   and row.get("outcome") in legacy]
        if choices:
            decided.append(choices)
    approved_first = sum(str(rows[0].get("action")) == "approve" for rows in decided)
    sent_back = sum(any(str(row.get("action")) in {"rework", "reject"} for row in rows) for rows in decided)
    return approved_first, sent_back, len(decided)


def _weekly_rows(weeks: list[datetime], week_counts: dict[datetime, dict[str, int]]) -> list[dict]:
    """Build the stable weekly output rows in chronological order."""
    return [{"week": week.date().isoformat(), "by_reason": week_counts[week], "total": sum(week_counts[week].values())}
            for week in weeks]


def _suggestions() -> list[dict[str, str]]:
    """Return the user-facing suggestion for each established wait reason."""
    return [{"reason": reason, "suggestion": SUGGESTIONS[reason]} for reason in REASONS]


def calculate_review_metrics(tickets: list[dict], events: list[dict], runs: list[dict], audits: list[dict],
                             now: datetime | None = None,
                             blocker_statuses: dict[str, str] | None = None) -> dict:
    """Return a stable set of counts and percentages; all periods use UTC calendar weeks."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    this_week = _week_start(now)
    weeks = [this_week - timedelta(weeks=offset) for offset in range(8, -1, -1)]
    week_counts = {week: {reason: 0 for reason in REASONS} for week in weeks}
    runs_by_key = _group_by_ticket(runs, "ticket_key", "started_at")
    ticket_by_key = _ticket_index(tickets, blocker_statuses)
    transitions, transition_times, ai_handoffs = _process_events(
        events, ticket_by_key, runs_by_key, weeks, week_counts, now)
    _process_audits(audits, ticket_by_key, runs_by_key, weeks, week_counts, now,
                    transitions, transition_times, ai_handoffs)
    _count_current_waits(ticket_by_key, transitions, runs_by_key, weeks, week_counts)
    audits_by_key = _group_by_ticket([row for row in audits if row.get("accepted")], "ticket", "ts")
    cohort_start = this_week - timedelta(weeks=8)
    finished_count, no_person_count = _finished_ticket_summary(
        tickets, transitions, audits_by_key, runs_by_key, cohort_start, now)
    waits = _wait_durations(transitions, ticket_by_key, cohort_start, now)
    approved_first, sent_back, decision_count = _decision_summary(audits_by_key, runs_by_key)
    current = week_counts[this_week]
    eight_weeks = week_counts[cohort_start]
    return {
        "people_needed_this_week": sum(current.values()),
        "people_needed_eight_weeks_ago": sum(eight_weeks.values()),
        "finished_with_no_person_pct": round(100 * no_person_count / finished_count, 1) if finished_count else None,
        "finished_tickets": finished_count,
        "median_wait_minutes": round(median(waits), 1) if waits else None,
        "approved_first_time_pct": round(100 * approved_first / decision_count, 1) if decision_count else None,
        "sent_back_or_rejected_pct": round(100 * sent_back / decision_count, 1) if decision_count else None,
        "decided_tickets": decision_count,
        "weekly": _weekly_rows(weeks, week_counts),
        "suggestions": _suggestions(),
    }
