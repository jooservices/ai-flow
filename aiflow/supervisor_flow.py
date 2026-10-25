"""Small stage-routing functions used by the legacy supervisor's main ticket loop."""
from __future__ import annotations

from typing import Any

from . import master_rescue

DAY = 86400


def stage_view(supervisor: Any, key: str, role: str, view: dict[str, Any]) -> dict[str, Any] | None:
    if role == "developer":
        agent, model = supervisor.ticket_spec(key)
        if agent:
            return {**view, "agent": agent, "spec_model": model, "agent_field": view.get("agent")}
    if role == "master_rescue":
        if view.get("status") != master_rescue.HANDOVER:
            return None
        return {**view, "agent_field": view.get("agent")}
    return view


def reserve_stage_agent(supervisor: Any, role: str, key: str,
                        view: dict[str, Any]) -> tuple[str | None, str | None]:
    if role == "master_rescue":
        return supervisor.master_worker.reserve(key)
    agent, owner = supervisor.reserve_agent(role, key, view.get("agent"))
    if agent is None:
        supervisor.unserved(key, role, view.get("agent"))
    return agent, owner


def process_ticket(supervisor: Any, issue: dict[str, Any], view: dict[str, Any], role: str,
                   agent: str | None, session_owner: str | None, branch_for: Any,
                   ticket_field: Any, log_message: Any) -> bool:
    key = issue["key"]
    description = view.get("description", "")
    project = supervisor.cfg.project(key)
    if issue["status"] == "To Do" and supervisor.incomplete(key, view):
        return True
    from .gitops import Repo
    repo = Repo(supervisor.cfg.root, ticket_field(description, "Repo", project.repo), project.company)
    change = supervisor.change_type(key)
    base, start = branch_for(project, change, str(view.get("type", "")), description)
    record_process_metadata(supervisor, key, issue, view, role, agent, change, log_message)
    stopped = prepare_repository(supervisor, key, repo, role)
    if stopped is not None:
        return stopped
    if reviewer_waits(supervisor, key, role, repo, log_message):
        return False
    return process_stage(supervisor, key, issue, view, role, agent, session_owner, repo, base, start)


def record_process_metadata(supervisor: Any, key: str, issue: dict[str, Any], view: dict[str, Any],
                            role: str, agent: str | None, change: str, log_message: Any) -> None:
    if change == "Hotfix":
        supervisor.redis.set(f"ishotfix:{key}", "1", 60 * DAY)
    if str(view.get("type", "")) == "Release":
        supervisor.redis.set(f"isrelease:{key}", "1", 60 * DAY)
    if role in ("developer", "reviewer", "master_rescue"):
        log_message(f"{key} [{issue['status']}] {role} -> {agent}")


def prepare_repository(supervisor: Any, key: str, repo: Any, role: str) -> bool | None:
    if not repo.exists():
        if role == "master_rescue":
            return supervisor.end_master_rescue(key, False, "Claude could not find the ticket repository")
        supervisor.block(key, f"Repository `{repo.name}` was not found under JOOservices/projects.", kind="Config")
        return True
    if not repo.has_remote():
        if role == "master_rescue":
            return supervisor.end_master_rescue(key, False, "Claude could not access the ticket repository remote")
        supervisor.block(key, f"Repository `{repo.name}` has no remote; the flow needs GitHub pull requests.", kind="Config")
        return True
    supervisor.redis.set(f"repo:{key}", repo.name, 30 * DAY)
    return None


def reviewer_waits(supervisor: Any, key: str, role: str, repo: Any, log_message: Any) -> bool:
    home = supervisor.redis.get(f"home:{key}")
    if role == "reviewer" and home and home != supervisor.cfg.machine and not repo.remote_branch(key):
        log_message(f"{key}: worktree lives on {home} and the branch was never pushed; leaving it")
        return True
    return False


def process_stage(supervisor: Any, key: str, issue: dict[str, Any], view: dict[str, Any], role: str,
                  agent: str | None, session_owner: str | None, repo: Any, base: str, start: str) -> bool:
    if role == "developer":
        return supervisor.develop(key, issue, view, repo, base, agent or "", start, session_owner)
    if role == "master_rescue":
        record = supervisor.master_worker.active_record(issue)
        if record is None:
            return False
        return supervisor.develop(key, issue, view, repo, base, master_rescue.RESCUE_AGENT,
                                  start, session_owner, record)
    if role == "reviewer":
        return supervisor.review(key, view, repo, base, agent or "", session_owner)
    if role == "pr":
        return supervisor.pr_step(key, view, repo, base)
    if role == "human":
        return supervisor.human_wait(key, repo, base)
    if role == "finalize":
        return supervisor.finalize(key, repo, base)
    return supervisor.gate(key, view, repo, base)
