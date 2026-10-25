"""Shared developer work sequence used by normal tickets and master-rescue pickup."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .gitops import GitError

DAY = 86400


def develop(supervisor: Any, key: str, issue: dict[str, Any], view: dict[str, Any], repo: Any,
            base: str, agent: str, start: str, session_owner: str | None,
            rescue_record: dict[str, Any] | None, log_message: Any) -> bool:
    if development_limit(supervisor, key, repo, rescue_record):
        return True
    started, previous = start_development(supervisor, key, issue, view, agent, log_message)
    if not started:
        return False
    prepared = prepare_worktree(supervisor, key, view, repo, base, start, agent, rescue_record)
    if prepared is None:
        return True
    workdir, branch = prepared
    start_run(supervisor, key, issue, view, agent, previous, workdir, branch)
    model_override, deadline = supervisor.master_worker.runtime(rescue_record) if rescue_record is not None else (None, None)
    result = supervisor.execute("developer", agent, key, view, workdir, branch, base, repo,
                                session_owner=session_owner, model_override=model_override,
                                rescue_deadline=deadline)
    why, text, found, quota = result
    if why == "lost-lease":
        return True
    if rescue_record is not None:
        return supervisor.finish_master_attempt(key, view, repo, base, agent, workdir, branch, text, why, found, quota)
    return finish_developer_attempt(supervisor, key, view, repo, base, agent, workdir, branch, text, why, found, quota)


def development_limit(supervisor: Any, key: str, repo: Any, rescue_record: dict[str, Any] | None) -> bool:
    if rescue_record is None and supervisor.attempts(key, "developer") >= supervisor.cfg.max_attempts:
        supervisor.block(key, "Too many failed developer attempts.", kind="Attempts")
        return True
    return supervisor.state(key, "isrelease") and not supervisor.release_ready(key, repo)


def start_development(supervisor: Any, key: str, issue: dict[str, Any], view: dict[str, Any],
                     agent: str, log_message: Any) -> tuple[bool, str]:
    if issue["status"] == "To Do" and not supervisor.jira.transition(key, "In Progress"):
        log_message(f"{key}: could not start (someone else did?)")
        return False, ""
    if issue["status"] == "To Do":
        supervisor.clear_block(key)
    if view.get("agent_field", view.get("agent")) != agent:
        supervisor.jira.set_agent(key, agent)
    if view.get("spec_model") and view.get("agent") == agent:
        supervisor.redis.set(f"devmodel:{key}", f"{agent}:{view['spec_model']}", 30 * DAY)
    else:
        supervisor.redis.delete(f"devmodel:{key}")
    supervisor.label_workspace(key)
    if "rework" in issue.get("labels", []):
        supervisor.jira.remove_label(key, "rework")
    previous = supervisor.redis.get(f"dev:{key}")
    supervisor.redis.set(f"dev:{key}", agent, 30 * DAY)
    supervisor.redis.set(f"home:{key}", supervisor.cfg.machine, 30 * DAY)
    return True, previous or ""


def prepare_worktree(supervisor: Any, key: str, view: dict[str, Any], repo: Any, base: str, start: str,
                     agent: str, rescue_record: dict[str, Any] | None) -> tuple[Path, str] | None:
    try:
        workdir, branch = repo.ensure_worktree(key, view.get("summary", key), start or base)
        repo.adopt_remote(workdir, branch)
    except GitError as error:
        if rescue_record is not None:
            supervisor.end_master_rescue(key, False, f"Claude could not prepare the ticket worktree ({type(error).__name__})")
        else:
            supervisor.failed(key, "developer", agent, "worktree", str(error))
        return None
    if not supervisor.prepare_ticket_database(key, workdir):
        if rescue_record is not None:
            supervisor.end_master_rescue(key, False, "Claude could not prepare the ticket test database")
        return None
    return workdir, branch


def start_run(supervisor: Any, key: str, issue: dict[str, Any], view: dict[str, Any], agent: str,
              previous: str, workdir: Path, branch: str) -> None:
    takeover = ""
    if issue["status"] == "In Progress" and previous:
        reason = "hit its usage limit" if supervisor.cooling(previous) else "stopped (died, lost its lease or showed no activity)"
        takeover = f" Taking over from {previous}, which {reason}; continuing from the branch and the comments above."
    kind = "rework" if supervisor.state(key, "rework") else "developer"
    supervisor.jira.comment(key, f"AI: {agent} on {supervisor.cfg.machine}   Branch: {branch}   Worktree: {workdir}\n"
                                  f"Now: starting {kind} run (resumes from existing commits if any).{takeover}")
    supervisor.replies_file(key).unlink(missing_ok=True)


def finish_developer_attempt(supervisor: Any, key: str, view: dict[str, Any], repo: Any, base: str, agent: str,
                             workdir: Path, branch: str, text: str, why: str,
                             found: tuple[str, str] | None, quota: bool) -> bool:
    if quota:
        supervisor.quota_hit(key, agent, "developer")
    elif found and found[0] == "DONE" and repo.ahead(workdir, base) > 0:
        supervisor.finish_development(key, view, repo, base, agent, workdir, branch, text)
    elif found and found[0] == "BLOCKED":
        supervisor.block(key, f"{agent} reported: {found[1]}", kind="Unclear")
    else:
        supervisor.failed(key, "developer", agent, why if why != "ok" else "no DONE result or no commits", text)
    return True
