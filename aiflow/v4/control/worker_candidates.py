"""Worker candidate ordering helpers."""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Any, Mapping


def diversify_dev_candidates(candidates: list[dict[str, Any]], account_id: str, worker_id: str,
                             project_priorities: Mapping[str, int],
                             ticket_priorities: Mapping[str, int]) -> list[dict[str, Any]]:
    """Rotate equally ranked development offers per worker session to reduce simultaneous claim collisions.

    The API already orders candidates by flow-bug status, project priority, rework status, ticket priority, and
    creation time. Rotate only contiguous dev groups that share the same priority dimensions. Within a group, poll
    projects in a stable worker-specific round robin while keeping each project's creation order. Stable hashing
    keeps polls repeatable and higher-priority groups ahead of lower-priority groups. An empty worker id preserves
    the original ordering for older clients.
    """
    ordered = list(candidates)
    if not worker_id:
        return ordered
    offset = 0
    while offset < len(ordered):
        first = ordered[offset]
        if first.get("step") != "dev":
            offset += 1
            continue
        labels = set(first.get("labels") or ())
        group_key = (
            project_priorities.get(str(first.get("project") or ""), 5),
            "rework" not in labels,
            ticket_priorities.get(str(first.get("priority") or ""), 2),
            "flow-bug" in labels,
        )
        end = offset + 1
        while end < len(ordered):
            item = ordered[end]
            if item.get("step") != "dev":
                break
            item_labels = set(item.get("labels") or ())
            item_key = (
                project_priorities.get(str(item.get("project") or ""), 5),
                "rework" not in item_labels,
                ticket_priorities.get(str(item.get("priority") or ""), 2),
                "flow-bug" in item_labels,
            )
            if item_key != group_key:
                break
            end += 1
        group = ordered[offset:end]
        if len(group) > 1:
            queues: dict[str, deque[dict[str, Any]]] = {}
            for item in group:
                queues.setdefault(str(item.get("project") or ""), deque()).append(item)
        else:
            queues = {}
        if len(queues) > 1:
            stable_key = f"{account_id}:{worker_id}:{group_key}".encode("utf-8")
            projects = sorted(
                queues,
                key=lambda project: hashlib.blake2s(stable_key + b":" + project.encode("utf-8"),
                                                    digest_size=8).digest(),
            )
            diversified = []
            while any(queues.values()):
                for project in projects:
                    if queues[project]:
                        diversified.append(queues[project].popleft())
            ordered[offset:end] = diversified
        offset = end
    return ordered
