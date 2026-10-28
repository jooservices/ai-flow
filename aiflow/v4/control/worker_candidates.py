"""Worker candidate ordering helpers."""
from __future__ import annotations

import hashlib
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


def diversify_dev_candidates(candidates: list[dict[str, Any]], account_id: str, worker_id: str,
                             project_priorities: Mapping[str, int],
                             ticket_priorities: Mapping[str, int]) -> list[dict[str, Any]]:
    """Spread same-minute development offers across worker sessions.

    The API already orders candidates by workflow stage, project priority, rework status, ticket priority, and
    creation time. Rotate only contiguous development groups with the same ranking and creation minute. This keeps
    older minute buckets and all higher-priority groups ahead while giving concurrent workers different tickets
    from a burst created in the same minute. Stable hashing makes each worker's order repeatable. An empty worker
    id preserves the original ordering for older clients.
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
            stable_key = f"{account_id}:{worker_id}:{rank}".encode("utf-8")
            ordered[offset:end] = sorted(
                group,
                key=lambda item: hashlib.blake2s(
                    stable_key + b":" + str(item.get("key") or "").encode("utf-8"), digest_size=8
                ).digest(),
            )
        offset = end
    return ordered
