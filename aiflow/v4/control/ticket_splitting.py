"""Intake ticket splitting, backed by Jira links and an idempotent saved plan."""
from __future__ import annotations

import hashlib
from typing import Any

from . import capacity
from .repositories import ticket_splits
from ..core.ticket_splitting import InvalidSplitPlan, TARGET_MINUTES, validate_plan

PARENT_LABEL = "aiflow-split-parent"
_CHILD_LABEL_EXCLUDES = {"aiflow-ready", PARENT_LABEL}


def _child_labels(parent: dict) -> list[str]:
    """Keep routing, review and area metadata; size and split labels describe the parent only."""
    return sorted({str(label) for label in parent.get("labels") or []
                   if str(label) not in _CHILD_LABEL_EXCLUDES
                   and not str(label).startswith("aiflow-split-")
                   and not str(label).lower().startswith(("size-", "size:"))})


def _estimate_children(parent: dict, plan: list[dict[str, str]], samples: list[dict]) -> list[dict]:
    labels = _child_labels(parent)
    estimates = []
    for index, item in enumerate(plan, 1):
        estimate = capacity.estimate({"type": "Story", "project": parent["project"],
                                      "summary": item["summary"], "description": item["description"],
                                      "labels": labels}, samples)
        high = float(estimate["time_minutes"]["high"])
        if high > TARGET_MINUTES:
            raise SplitError(f"split ticket {index} is estimated at {high:g} minutes, above the 30-minute target")
        estimates.append(estimate)
    return estimates


class SplitError(InvalidSplitPlan):
    """A split request is invalid or the ticket does not need splitting."""


def _marker(parent_key: str, index: int) -> str:
    digest = hashlib.sha256(parent_key.encode("utf-8")).hexdigest()[:12]
    return f"aiflow-split-{digest}-{index:02d}"


def _find_child(jira, project: str, marker: str) -> str:
    jql_project = project.replace('"', '\\"')
    rows = jira.search(f'project = "{jql_project}" AND labels = "{marker}"', fields=["key", "labels"])
    keys = sorted({str(row.get("key") or "") for row in rows if row.get("key")})
    if len(keys) > 1:
        raise SplitError("multiple Jira tickets carry the same split marker")
    return keys[0] if keys else ""


def _has_link(jira, blocker: str, blocked: str, kind: str) -> bool:
    issue = next(iter(jira.search(f'key = "{blocker}"', fields=["key", "issuelinks"])), {})
    for link in (issue.get("fields") or {}).get("issuelinks") or []:
        link_type = (link.get("type") or {}).get("name")
        outward = (link.get("outwardIssue") or {}).get("key")
        inward = (link.get("inwardIssue") or {}).get("key")
        if link_type == kind and outward == blocker and inward == blocked:
            return True
        if kind == "Relates" and link_type == kind and {outward, inward} == {blocker, blocked}:
            return True
    return False


def _ensure_link(jira, blocker: str, blocked: str, kind: str) -> None:
    if not _has_link(jira, blocker, blocked, kind):
        jira.link_issues(blocker, blocked, kind)


def _ticket_row(connection, key: str) -> dict:
    parent = ticket_splits.ticket(connection, key)
    if not parent:
        raise SplitError("ticket is not in the control registry")
    return parent


def _create_children(ctx, connection, parent: dict, plan: list[dict[str, str]]) -> list[str]:
    jira, child_keys = ctx.jira, []
    labels = _child_labels(parent)
    for index, item in enumerate(plan, 1):
        marker = _marker(parent["key"], index)
        key = _find_child(jira, parent["project"], marker)
        if not key:
            key = jira.create_issue(parent["project"], item["summary"], item["description"], "Story",
                                    labels=[*labels, marker])
        else:
            for label in labels:
                jira.add_label(key, label)
        previous = child_keys[-1] if child_keys else ""
        _ensure_link(jira, parent["key"], key, "Relates")
        if previous:
            _ensure_link(jira, previous, key, "Blocks")
        child_keys.append(key)
        blocked_by = [previous] if previous else []
        ticket_splits.save_child(connection, key, parent, marker, item, blocked_by, labels)
    return child_keys


def _offer_children(ctx, connection, child_keys: list[str]) -> None:
    """Make the complete, linked plan offerable only after every child exists."""
    for key in child_keys:
        ctx.jira.add_label(key, "aiflow-ready")
        status = ctx.jira.transition_to(key, "To Do")
        ticket_splits.offer_child(connection, key, status)


def split_ticket(ctx, run: dict, requested_plan: Any) -> dict[str, Any]:
    """Serialize a parent split; child markers and saved plans make retries resume safely."""
    lock_key = "aiflow4-ticket-split:" + run["ticket_key"]
    with ctx.db.pool.connection() as connection:
        connection.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (lock_key,))
        connection.commit()
        try:
            result = _split_locked(ctx, connection, run, requested_plan)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock_key,))
            connection.commit()


def _split_locked(ctx, connection, run: dict, requested_plan: Any) -> dict[str, Any]:
    """Persist and apply the split once while the parent-specific lock is held."""
    parent = _ticket_row(connection, run["ticket_key"])
    saved = ticket_splits.plan(connection, parent["key"])
    if saved and saved["status"] == "complete":
        return {"parent": parent["key"], "children": list(saved["child_keys"] or []),
                "estimate": saved["estimate"], "already_split": True}
    if not saved:
        if parent["status"] != "Intake":
            raise SplitError("only an Intake ticket can be split")
        estimate = capacity.estimate(parent, capacity.history(connection))
        if float(estimate["time_minutes"]["high"]) <= TARGET_MINUTES:
            raise SplitError("ticket estimate is within the 30-minute target")
        plan = validate_plan(requested_plan)
        _estimate_children(parent, plan, capacity.history(connection))
        saved = ticket_splits.save_plan(connection, parent, estimate, TARGET_MINUTES, plan)
        connection.commit()
    else:
        plan = validate_plan(saved["plan"])
        _estimate_children(parent, plan, capacity.history(connection))
    plan = validate_plan(saved["plan"])
    child_keys = _create_children(ctx, connection, parent, plan)
    # Publish every child and ordering blocker in the registry before Jira labels can make children ready.
    connection.commit()
    jira = ctx.jira
    # Keep the parent in Intake and children unready until the full plan and its ordering links exist.
    _offer_children(ctx, connection, child_keys)
    jira.remove_label(parent["key"], "aiflow-ready")
    jira.add_label(parent["key"], PARENT_LABEL)
    jira.comment(parent["key"], "AI Flow split this ticket into ordered work: " + " → ".join(child_keys))
    status = jira.transition_to(parent["key"], "Backlog")
    ticket_splits.complete(connection, parent, child_keys, status, PARENT_LABEL)
    return {"parent": parent["key"], "children": child_keys, "estimate": saved["estimate"],
            "already_split": False}
