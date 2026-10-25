"""Work-time and ticket-fact snapshots used by the legacy operator's master rescue."""
from __future__ import annotations

import json
import math
from typing import Any

from . import events, master_rescue
from .jira import JiraError
from .lock import RedisError


def ticket_snapshot(issue: dict[str, Any], blocked: dict[str, Any], rounds: int, minutes_in_status: float,
                    worked: Any, rescue: Any, redis: Any, jira: Any) -> dict[str, Any]:
    """Add rescue eligibility, owner-only context, and the existing operator comment snapshot."""
    key, status = issue["key"], issue["status"]
    summary = str(issue.get("summary", ""))
    reason = str(blocked.get("reason") or summary)
    description, comments = owner_context(issue, reason, summary, redis, jira)

    stretch = minutes_in_status if status == "In Progress" else 0
    worked_minutes = _number(worked) + (minutes_in_status if status in master_rescue.WORK_STATUSES else 0)
    return {"blocked_kind": blocked.get("kind", ""), "reason": reason, "summary": summary,
            "description": description, "stretch_minutes": stretch, "worked_minutes": worked_minutes,
            "rescued": rescue is not None, "last_comments": comments,
            "rework_rounds": rounds}


def owner_context(issue: dict[str, Any], reason: str, summary: str, redis: Any, jira: Any) -> tuple[str, list[dict[str, Any]]]:
    """Read owner-classification text, reusing a Jira-updated cache across operator cycles."""
    key, updated = issue["key"], str(issue.get("updated", ""))
    cache_key = f"operator:rescue-context:{key}"
    cached = _cached_context(redis, cache_key, updated)
    if cached is not None:
        return cached
    description, comments, complete = _read_owner_context(issue, reason, summary, jira)
    if complete:
        _store_context(redis, cache_key, updated, description, comments)
    return description, comments


def _cached_context(redis: Any, cache_key: str, updated: str) -> tuple[str, list[dict[str, Any]]] | None:
    try:
        cached = json.loads(redis.get(cache_key) or "{}")
    except (TypeError, ValueError, RedisError):
        cached = {}
    if isinstance(cached, dict) and cached.get("updated") == updated and isinstance(cached.get("comments"), list):
        return str(cached.get("description", "")), cached["comments"]
    return None


def _read_owner_context(issue: dict[str, Any], reason: str, summary: str, jira: Any) -> tuple[str, list[dict[str, Any]], bool]:
    key = issue["key"]
    description = str(issue.get("description", "") or "")
    description_loaded = bool(description)
    if not description:
        if master_rescue.owner_only_topic(reason, summary):
            description_loaded = True
        else:
            try:
                description = str(jira.view(key, full=True).get("description", "") or "")
                description_loaded = True
            except (JiraError, ValueError, TypeError):
                pass
    try:
        comments = jira.comments(key, 3)
        comments_loaded = isinstance(comments, list)
    except (JiraError, ValueError, TypeError):
        comments, comments_loaded = issue.get("last_comments", []), False
    if not isinstance(comments, list):
        comments = []
    return description, comments, description_loaded and comments_loaded


def _store_context(redis: Any, cache_key: str, updated: str, description: str, comments: list[dict[str, Any]]) -> None:
    try:
        redis.set(cache_key, json.dumps({"updated": updated, "description": description, "comments": comments}), 60)
    except RedisError:
        pass


def _number(value: Any) -> float:
    try:
        parsed = float(value or 0)
        return max(0.0, parsed) if math.isfinite(parsed) else 0.0
    except (TypeError, ValueError):
        return 0.0


def add_work_time(redis: Any, key: str, previous: str, since: Any, now: float) -> None:
    """Accumulate completed In Progress/In Review time; CI and other statuses do not count."""
    if previous not in master_rescue.WORK_STATUSES or not since:
        return
    try:
        worked = float(redis.get(f"worked:{key}") or 0) + max(0.0, now - float(since)) / 60
    except (TypeError, ValueError):
        return
    redis.set_persistent(f"worked:{key}", str(round(worked, 2)))


def record_status_change(redis: Any, key: str, to: str, previous: str, source: str, since: Any, now: float) -> None:
    """Emit a status event and persist completed work time before the next status snapshot."""
    events.emit("ticket.status", ticket=key, to_status=to, from_status=previous, source=source,
                minutes_in_from=round((now - float(since)) / 60, 2) if since else None)
    add_work_time(redis, key, previous, since, now)
