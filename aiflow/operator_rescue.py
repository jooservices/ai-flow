"""Work-time and ticket-fact snapshots used by the legacy operator's master rescue."""
from __future__ import annotations

import json
import math
import subprocess
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
    description, comments, context_complete = owner_context(issue, reason, summary, redis, jira)

    stretch = minutes_in_status if status == "In Progress" else 0
    worked_minutes = _number(worked) + (minutes_in_status if status in master_rescue.WORK_STATUSES else 0)
    return {"blocked_kind": blocked.get("kind", ""), "reason": reason, "summary": summary,
            "description": description, "stretch_minutes": stretch, "worked_minutes": worked_minutes,
            "rescued": rescue is not None, "last_comments": comments,
            "context_complete": context_complete,
            "rework_rounds": rounds}


def owner_context(issue: dict[str, Any], reason: str, summary: str, redis: Any, jira: Any) -> tuple[str, list[dict[str, Any]], bool]:
    """Read owner-classification text, reusing a Jira-updated cache across operator cycles."""
    key, updated = issue["key"], str(issue.get("updated", ""))
    cache_key = f"operator:rescue-context:{key}"
    cached = _cached_context(redis, cache_key, updated)
    if cached is not None:
        return cached[0], cached[1], True
    description, comments, complete = _read_owner_context(issue, reason, summary, jira)
    if complete:
        _store_context(redis, cache_key, updated, description, comments)
    return description, comments, complete


def _cached_context(redis: Any, cache_key: str, updated: str) -> tuple[str, list[dict[str, Any]]] | None:
    try:
        cached = json.loads(redis.get(cache_key) or "{}")
    except (TypeError, ValueError, RedisError):
        cached = {}
    if (isinstance(cached, dict) and cached.get("updated") == updated and cached.get("complete") is True
            and isinstance(cached.get("comments"), list)):
        return str(cached.get("description", "")), cached["comments"]
    return None


def _read_owner_context(issue: dict[str, Any], reason: str, summary: str, jira: Any) -> tuple[str, list[dict[str, Any]], bool]:
    description, description_loaded = _description_context(issue, reason, summary, jira)
    comments, comments_loaded = _comment_context(issue, jira)
    return description, comments, description_loaded and comments_loaded


def _description_context(issue: dict[str, Any], reason: str, summary: str, jira: Any) -> tuple[str, bool]:
    raw_description = issue.get("description", "")
    description = raw_description if isinstance(raw_description, str) else ""
    if isinstance(raw_description, str) and description:
        return description, True
    if master_rescue.owner_only_topic(reason, summary):
        return description, True
    try:
        viewed = jira.view(issue["key"], full=True)
    except (JiraError, ValueError, TypeError, OSError, subprocess.TimeoutExpired):
        return "", False
    if not isinstance(viewed, dict):
        return "", False
    viewed_description = viewed.get("description", "")
    if viewed_description is not None and not isinstance(viewed_description, str):
        return "", False
    return str(viewed_description or ""), True


def _comment_context(issue: dict[str, Any], jira: Any) -> tuple[list[dict[str, Any]], bool]:
    try:
        comments = jira.comments(issue["key"], 3)
    except (JiraError, ValueError, TypeError, OSError, subprocess.TimeoutExpired):
        comments = issue.get("last_comments", [])
        return (comments if isinstance(comments, list) else []), False
    loaded = isinstance(comments, list) and all(
        isinstance(comment, dict) and isinstance(comment.get("body"), str) for comment in comments)
    if not isinstance(comments, list):
        comments = []
    return comments, loaded


def _store_context(redis: Any, cache_key: str, updated: str, description: str, comments: list[dict[str, Any]]) -> None:
    try:
        redis.set(cache_key, json.dumps({"updated": updated, "description": description, "comments": comments,
                                         "complete": True}), 60)
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
