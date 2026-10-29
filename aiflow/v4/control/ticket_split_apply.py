"""Apply a validated intake split plan to Jira once."""
from __future__ import annotations

import logging
from typing import Any

from ..core.ticket_splitting import validate_plan

log = logging.getLogger("aiflow4.split")
PARENT_LABEL = "aiflow-split-parent"


def child_label(parent_key: str, index: int) -> str:
    """Stable per-index label so a second apply finds the same child."""
    return f"aiflow-split-{parent_key.lower()}-{index}"


def apply_plan(jira: Any, parent_key: str, project: str, plan: Any) -> dict[str, Any]:
    """Create linked Story children for ``plan``. Idempotent by child_label. No aiflow-ready on children."""
    children = validate_plan(plan)
    keys: list[str] = []
    created: list[bool] = []
    for index, child in enumerate(children, 1):
        label = child_label(parent_key, index)
        existing = _existing_key(jira, project, label)
        if existing:
            keys.append(existing)
            created.append(False)
            continue
        key = jira.create_issue(
            project, child["summary"], child["description"], issuetype="Story", labels=[label]
        )
        jira.link_issues(key, parent_key, "Relates")
        keys.append(key)
        created.append(True)
    for index in range(len(keys) - 1):
        if created[index + 1]:
            jira.link_issues(keys[index], keys[index + 1], "Blocks")
    jira.remove_label(parent_key, "aiflow-ready")
    jira.add_label(parent_key, PARENT_LABEL)
    return {"parent": parent_key, "children": keys}


def after_intake_result(ctx: Any, run: dict[str, Any], body: Any) -> None:
    """Apply a split_plan from a finished intake run. No-op for every other result."""
    if run.get("step") != "intake" or getattr(body, "outcome", None) != "split_plan":
        return
    if not getattr(ctx, "jira", None):
        return
    details = getattr(body, "details", None) or {}
    plan = details.get("split_plan") if isinstance(details, dict) else None
    if plan is None:
        return
    apply_plan(ctx.jira, str(run["ticket_key"]), str(run["project"]), plan)


def _existing_key(jira: Any, project: str, label: str) -> str | None:
    jql = f'project = {project} AND labels = "{label}"'
    for issue in jira.search(jql):
        key = issue.get("key") if isinstance(issue, dict) else None
        if key:
            return str(key)
    return None
