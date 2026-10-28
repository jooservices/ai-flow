"""Worker candidate ordering helpers."""
from __future__ import annotations

import hashlib
import re
from collections import deque
from datetime import datetime
from typing import Any, Mapping


def _minute_bucket(value: Any) -> str | None:
    """Return a stable minute bucket for database datetimes or API timestamp strings."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        return None
    return stamp.replace(second=0, microsecond=0).isoformat()


def _rank(item: Mapping[str, Any], project_priorities: Mapping[str, int],
          ticket_priorities: Mapping[str, int]) -> tuple[Any, ...] | None:
    if item.get("step") != "dev":
        return None
    labels = set(item.get("labels") or ())
    minute = _minute_bucket(item.get("jira_created"))
    if minute is None:
        return None
    return (
        project_priorities.get(str(item.get("project") or ""), 5),
        "rework" not in labels,
        ticket_priorities.get(str(item.get("priority") or ""), 2),
        "flow-bug" in labels,
        minute,
    )


def _project_offset(account_id: str, worker_id: str, rank: tuple[Any, ...], size: int) -> int:
    """Give stable worker identities different starting projects within a tied queue."""
    match = re.fullmatch(r"(.*?)(\d+)(?:\.(\d+))?", worker_id)
    if match:
        prefix, number, instance = match.groups()
        # Account-N.2 is the second session of account-N, so start it halfway around the project ring.
        ordinal = int(number) + (size // 2 if instance else 0)
        seed = f"{prefix}:{rank}".encode("utf-8")
    else:
        ordinal = 0
        seed = f"{account_id}:{worker_id}:{rank}".encode("utf-8")
    start = int.from_bytes(hashlib.blake2s(seed, digest_size=8).digest(), "big") % size
    return (start + ordinal) % size


def diversify_dev_candidates(candidates: list[dict[str, Any]], account_id: str, worker_id: str,
                             project_priorities: Mapping[str, int],
                             ticket_priorities: Mapping[str, int]) -> list[dict[str, Any]]:
    """Spread same-minute development offers across worker sessions.

    The API already orders candidates by workflow stage, project priority, rework status, ticket priority, and
    creation time. Rotate only contiguous development groups with the same ranking and creation minute. Within each
    group, projects are interleaved per worker while each project's ticket order stays unchanged. Older minute
    buckets and higher-priority groups stay ahead. Stable hashing makes each worker's order repeatable. An empty
    worker id preserves the original ordering for older clients.
    """
    ordered = list(candidates)
    if not worker_id:
        return ordered

    offset = 0
    while offset < len(ordered):
        rank = _rank(ordered[offset], project_priorities, ticket_priorities)
        if rank is None:
            offset += 1
            continue
        end = offset + 1
        while end < len(ordered) and _rank(ordered[end], project_priorities, ticket_priorities) == rank:
            end += 1
        group = ordered[offset:end]
        if len(group) > 1:
            queues: dict[str, deque[dict[str, Any]]] = {}
            for item in group:
                queues.setdefault(str(item.get("project") or ""), deque()).append(item)
            projects = sorted(queues)
            start = _project_offset(account_id, worker_id, rank, len(projects))
            projects = projects[start:] + projects[:start]
            diversified = []
            while any(queues.values()):
                for project in projects:
                    if queues[project]:
                        diversified.append(queues[project].popleft())
            ordered[offset:end] = diversified
        offset = end
    return ordered
