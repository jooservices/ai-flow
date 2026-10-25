"""Ticket and CI projections for the legacy operator's incident snapshot."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from typing import Any

from . import github, operator_rescue
from .github import GitHub, GitHubError
from .gitops import GitError, Repo
from .incidents import WORKED


def epoch(value: str, default: float) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return default


def tickets(operator: Any, now: float) -> list[dict[str, Any]]:
    projects = ", ".join(operator.cfg.projects)
    statuses = ", ".join(f'"{status}"' for status in ("To Do", "Blocked", *WORKED))
    issues = operator.jira.search(
        f"project in ({projects}) AND status in ({statuses}) AND issuetype != Epic ORDER BY created ASC", 200)
    parents = operator.sup.parents_of_open_work(issues)
    issues = [issue for issue in issues if issue["key"] not in parents]
    leased = {name.split(":", 1)[1] for name in operator.redis.scan("lease:*")}
    values = _ticket_values(operator, issues)
    return [ticket(operator, issue, detail, leased, now) for issue, detail in zip(issues, values)]


def _ticket_values(operator: Any, issues: list[dict[str, Any]]) -> zip:
    keys = [issue["key"] for issue in issues]
    fetch = lambda prefix: operator.redis.get_many([f"{prefix}:{key}" for key in keys]) if keys else []
    return zip(fetch("blocked"), fetch("cycles"), fetch("statussince"), fetch("worked"), fetch("rescue"),
               fetch("pr"), fetch("gateentry"), fetch("pushed"))


def ticket(operator: Any, issue: dict[str, Any], values: tuple[Any, ...], leased: set[str], now: float) -> dict[str, Any]:
    blocked, rounds, since, worked, rescue_record, number, entry, pushed = values
    key, status = issue["key"], issue["status"]
    updated = epoch(issue.get("updated", ""), now)
    status_since = _float_or(since, updated)
    minutes_in_status = max(0, (now - status_since) / 60)
    gate_since = _gate_since(entry, pushed, status_since)
    waiting_for_ci = max(0, (now - gate_since) / 60)
    blocked_data = _json_dict(blocked)
    rescue_rounds = _rounds(rounds, blocked_data)
    rescue = operator_rescue.ticket_snapshot(issue, blocked_data, rescue_rounds, minutes_in_status, worked,
                                             rescue_record, operator.redis, operator.jira)
    pull_request, ci_state = inspect_ci(operator, key, status, key in leased, number,
                                        minutes_in_status, waiting_for_ci)
    return dict(key=key, status=status, status_since=status_since, updated=updated,
                labels=issue.get("labels", []), leased=key in leased, blocked_kind=rescue["blocked_kind"],
                reason=rescue["reason"], summary=rescue["summary"], description=rescue["description"],
                stretch_minutes=rescue["stretch_minutes"], worked_minutes=rescue["worked_minutes"],
                rescued=rescue["rescued"], context_complete=rescue["context_complete"],
                cycles=rescue_rounds, rework_rounds=rescue_rounds, last_comments=rescue["last_comments"],
                attempts=rescue["attempts"],
                loop_limit=operator.cfg.loop_limit_for(key), pull_request=pull_request, ci_state=ci_state,
                ci_since=gate_since, minutes_waiting_for_ci=int(waiting_for_ci))


def inspect_ci(operator: Any, key: str, status: str, leased: bool, number: Any,
               minutes_in_status: float, waiting_for_ci: float) -> tuple[dict[str, Any] | None, str]:
    pull_request = {"number": number} if number else None
    ci_state = "unknown"
    inspect = waiting_for_ci >= operator.policy.ci_wait_minutes or minutes_in_status >= operator.policy.stuck_minutes
    if status != "In QA" or leased or not number or not inspect:
        return pull_request, ci_state
    try:
        project = operator.cfg.project(key)
        repo = Repo(operator.cfg.root, project.repo, project.company)
        pr = GitHub(repo.github_slug()).view(int(number))
        ci_state, failing = github.checks_state(pr.get("statusCheckRollup"))
        return {"number": int(number), "url": pr.get("url", ""), "state": pr.get("state", ""),
                "head": pr.get("headRefName", ""), "ci_state": ci_state, "failing_checks": failing}, ci_state
    except (GitError, GitHubError, subprocess.TimeoutExpired, ValueError, OSError):
        # Missing repository or GitHub data is not evidence that CI is still waiting.
        return pull_request, ci_state


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value) if value else default
    except (TypeError, ValueError):
        return default


def _gate_since(entry: Any, pushed: Any, status_since: float) -> float:
    values = []
    for value in (entry, pushed):
        try:
            if value:
                values.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(values) if values else status_since


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        found = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _rounds(value: Any, blocked: dict[str, Any]) -> int:
    try:
        return int(value or blocked.get("rework_rounds", 0) or 0)
    except (TypeError, ValueError):
        return 0
