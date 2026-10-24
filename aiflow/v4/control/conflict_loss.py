"""Work lost to merge conflicts, for Command Center (docs/v4/merge-conflicts.md).

Definitions (shown on the screen as well, so the number is not misread):

  share   verifications sent back for rework only because the branch conflicts with the base branch, divided by all
          completed verifications of the period (merged, handed to a person, reworked or failed). A verification
          that failed for a conflict and another reason counts once, as a conflict loss: verify stops at the conflict.
  time    the summed run duration of those verifications, of the dev rework runs they triggered and of the
          re-verifications that followed: the repeated work of "another development run and a new verification".
          A run that belongs to two conflicts (a re-verification that conflicts again) is counted once.
  clean   a branch that AI Flow updated itself with a clean merge is not a loss and is never counted; the number of
          such verifications is reported next to the share.

A conflict verification is a verify run with outcome `rework` whose summary starts with "conflicts with " (the
reason the verify worker records) or whose details say `reason: conflict`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

COMPLETED = ("merged", "human", "rework", "failed")
DEFINITION = ("Share lost to conflicts: verifications sent back for rework only because the branch conflicts with "
              "the base branch, divided by all completed verifications (merged, handed to a person, reworked, failed). "
              "Time spent: those verifications plus the dev rework runs they triggered and the re-verifications "
              "that followed. Branches that AI Flow updates automatically with a clean merge are not counted.")


def is_conflict(run: dict) -> bool:
    return (run["step"] == "verify" and run["outcome"] == "rework"
            and (str(run.get("summary") or "").startswith("conflicts with ")
                 or (run.get("details") or {}).get("reason") == "conflict"))


def _seconds(run: dict) -> float:
    if not run.get("ended_at") or not run.get("started_at"):
        return 0.0
    return max(0.0, (run["ended_at"] - run["started_at"]).total_seconds())


def repeated_work(conflict: dict, ticket_runs: list[dict]) -> list[dict]:
    """The conflict verification, the next dev run after it and the next verification after that dev run."""
    later = [r for r in ticket_runs if r["started_at"] > conflict["started_at"] and r.get("ended_at")]
    dev = next((r for r in later if r["step"] == "dev"), None)
    out = [conflict]
    if dev:
        out.append(dev)
        again = next((r for r in later if r["step"] == "verify" and r["started_at"] > dev["started_at"]), None)
        if again:
            out.append(again)
    return out


def _period_key(day: date, bucket: str) -> str:
    return (day - timedelta(days=day.weekday())).isoformat() if bucket == "week" else day.isoformat()


def _totals(verifications: list[dict], conflicts: list[dict], by_ticket: dict[str, list[dict]]) -> dict[str, Any]:
    seen: dict[str, float] = {}
    for conflict in conflicts:
        for run in repeated_work(conflict, by_ticket.get(conflict["ticket_key"], [])):
            seen[str(run["id"])] = _seconds(run)
    seconds = sum(seen.values())
    done = len(verifications)
    return {"verifications": done, "conflicts": len(conflicts),
            "share_percent": round(100 * len(conflicts) / done, 1) if done else 0.0,
            "seconds": round(seconds), "average_seconds": round(seconds / len(conflicts)) if conflicts else 0,
            "clean_updates": sum(1 for v in verifications if (v.get("details") or {}).get("auto_updated"))}


def calculate(runs: Iterable[dict], now: datetime, days: int = 14, bucket: str = "day") -> dict[str, Any]:
    """Share, time and trend from verify and dev runs (columns id, ticket_key, step, outcome, summary, details,
    started_at, ended_at). The current period is the last `days` days; the previous one is the `days` before it."""
    runs = sorted(runs, key=lambda r: r["started_at"])
    by_ticket: dict[str, list[dict]] = {}
    for run in runs:
        by_ticket.setdefault(run["ticket_key"], []).append(run)
    today = now.astimezone(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    previous_start = start - timedelta(days=days)

    def window(first: date, last: date) -> tuple[list[dict], list[dict]]:
        done = [r for r in runs if r["step"] == "verify" and r["outcome"] in COMPLETED
                and first <= r["started_at"].astimezone(timezone.utc).date() <= last]
        return done, [r for r in done if is_conflict(r)]

    current = _totals(*window(start, today), by_ticket)
    previous = _totals(*window(previous_start, start - timedelta(days=1)), by_ticket)
    trend: dict[str, dict] = {}
    cursor = start
    while cursor <= today:
        trend.setdefault(_period_key(cursor, bucket), {"period": _period_key(cursor, bucket), "verifications": 0,
                                                      "conflicts": 0, "seconds": 0, "share_percent": 0.0})
        cursor += timedelta(days=1)
    done, _ = window(start, today)
    for row in trend.values():
        members = [r for r in done if _period_key(r["started_at"].astimezone(timezone.utc).date(), bucket) == row["period"]]
        losses = [r for r in members if is_conflict(r)]
        part = _totals(members, losses, by_ticket)
        row.update(verifications=part["verifications"], conflicts=part["conflicts"], seconds=part["seconds"],
                   share_percent=part["share_percent"])
    return {"days": days, "bucket": bucket, "definition": DEFINITION, "current": current, "previous": previous,
            "change": {"share_points": round(current["share_percent"] - previous["share_percent"], 1),
                       "seconds": current["seconds"] - previous["seconds"]},
            "trend": list(trend.values())}


def summary(db, days: int = 14, bucket: str = "day", project_keys: list[str] | tuple[str, ...] | None = None,
            project: str = "") -> dict[str, Any]:
    """Read the runs of the current and previous period (plus the repeated work that follows) and calculate."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=2 * days + 1)
    keys = list(project_keys) if project_keys is not None else None
    runs = db.all(
        "SELECT id, ticket_key, project, step, outcome, summary, details, started_at, ended_at FROM runs"
        " WHERE step IN ('verify', 'dev') AND status <> 'running' AND started_at >= %s"
        " AND (%s::text = '' OR project = %s) AND (%s::text[] IS NULL OR project = ANY(%s))",
        (since, project, project, keys, keys))
    return calculate(runs, now, days, bucket)
