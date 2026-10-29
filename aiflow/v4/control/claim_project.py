"""Build the project payload returned by POST /v1/work/claim."""
from __future__ import annotations

from typing import Any

from ..core import work_limits
from ..core.work_limits import retry_hard_minutes
from .repositories import developer_runs


def payload(project, selected_repo: dict, node: str, step: str, db, ticket_key: str) -> dict[str, Any]:
    """Project settings for a claimed run, with a raised hard limit after developer timeouts."""
    if step == "master":
        return {"key": "", "repo": "", "base": "", "hotfix_base": "", "checks": [], "stack": "",
                "node": node, "work_minutes": dict(work_limits.DEFAULTS),
                "merge": {"method": "squash", "auto": False, "require_human": True}}
    if not project:
        return {}
    limits = dict(project.work_minutes())
    if step == "dev":
        limits["hard"] = retry_hard_minutes(limits["hard"], developer_runs.timeout_count(db, ticket_key))
    environment = []
    if step in ("intake", "dev", "verify", "post_merge"):
        environment = [item.public() for item in project.environment_items]
    merge = project.merge or {}
    return {"key": project.key, **selected_repo, "hotfix_base": project.hotfix_base,
            "work_minutes": limits, "environment": environment,
            "merge": {"method": merge.get("method", "squash"),
                      "auto": bool(merge.get("auto", True)),
                      "require_human": bool(merge.get("require_human", False))}}
