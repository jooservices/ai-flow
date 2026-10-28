"""Worker candidate ordering helpers."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping


def diversify_dev_candidates(candidates: list[dict[str, Any]], account_id: str,
                             project_priorities: Mapping[str, int],
                             ticket_priorities: Mapping[str, int]) -> list[dict[str, Any]]:
    """Rotate equally ranked development offers per account to reduce simultaneous claim collisions.

    The API already orders candidates by flow-bug status, project priority, rework status, ticket priority, and
    creation time. Rotate only contiguous dev groups that share the same priority dimensions, keeping every offer
    eligible and every higher-priority group ahead of a lower-priority group. Stable hashing keeps polls repeatable.
    """
    ordered = list(candidates)
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
            stable_key = f"{account_id}:{group_key}".encode("utf-8")
            rotation = int.from_bytes(hashlib.blake2s(stable_key, digest_size=8).digest(), "big") % len(group)
            ordered[offset:end] = group[rotation:] + group[:rotation]
        offset = end
    return ordered
