"""Shared prompt setup, timed process execution, and result handling for the legacy supervisor."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import agents, events, runner, usage
from .config import PROJECT_DIR
from .gitops import GitError
from .jira import JiraError
from .lock import RedisError


def execute(supervisor: Any, role: str, agent: str, key: str, view: dict[str, Any], workdir: Path,
            branch: str, base: str, repo: Any, slot_owner: str | None, session_owner: str | None,
            model_override: tuple[str, str] | None, rescue_deadline: float | None,
            slot_seconds: int, log_message: Any) -> tuple[str, str, tuple[str, str] | None, bool]:
    prepared = prepare(supervisor, role, agent, key, view, workdir, branch, base, repo, model_override)
    argv, stdin_text, log_file, result_file, model, effort = prepared
    beat = heartbeat(supervisor, role, key, agent, workdir, branch, repo, slot_owner, session_owner,
                     slot_seconds, log_message)
    began = time.monotonic()
    if session_owner and not supervisor.redis.renew_session(supervisor.cfg.machine, agent, session_owner,
                                                              supervisor.cfg.lease_seconds):
        return "lost-lease", "", None, False
    env = agents.run_env(agent, workdir, supervisor.cfg.root, supervisor.memory_env(key, repo))
    max_seconds = supervisor.master_worker.execution_seconds(rescue_deadline, supervisor.cfg.max_minutes * 60)
    if rescue_deadline is not None and max_seconds <= 0:
        return "rescue budget expired during worker setup", "", None, False
    code, why = runner.run(argv, stdin_text, workdir, log_file, env,
                           idle_seconds=supervisor.cfg.idle_minutes * 60, max_seconds=max_seconds,
                           heartbeat_seconds=supervisor.cfg.heartbeat_seconds, heartbeat=beat)
    return finish(supervisor, role, agent, key, repo, workdir, log_file, result_file, model, effort,
                  began, code, why, log_message)


def prepare(supervisor: Any, role: str, agent: str, key: str, view: dict[str, Any], workdir: Path,
            branch: str, base: str, repo: Any,
            model_override: tuple[str, str] | None) -> tuple[list[str], str | None, Path, Path, str, str]:
    description = view.get("description", "")
    try:
        comments = "\n".join(f"- {comment.get('author', '?')} {comment.get('created', '')}: "
                              f"{str(comment.get('body', ''))[:1500]}"
                              for comment in supervisor.jira.comments(key, 6)) or "(none)"
    except (JiraError, ValueError, TypeError):
        comments = "(unavailable)"
    values = dict(machine=supervisor.cfg.machine, key=key, summary=view.get("summary", ""), role=role,
                  agent=agent, repo=repo.name, worktree=str(workdir), branch=branch, base=base,
                  root=str(supervisor.cfg.root), description=description, comments=comments,
                  rework=supervisor.rework_text(key), pr=supervisor.pr_text(key),
                  replies_file=str(supervisor.replies_file(key)),
                  type=supervisor.change_type(key) or str(view.get("type", "")),
                  parent=view.get("parent") or "none", version=supervisor.state(key, "version"),
                  last_tag=supervisor.state(key, "lasttag") or "(none yet)",
                  release_check=supervisor.cfg.project(key).release_check or "(none configured)",
                  project=key.rsplit("-", 1)[0], memory=supervisor.recall(key, view, repo))
    prompt_role = "release" if role == "developer" and supervisor.state(key, "isrelease") else role
    prompt = "\n\n".join((PROJECT_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")
                         for name in ("common", prompt_role))
    for name, value in values.items():
        prompt = prompt.replace("{" + name + "}", str(value))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_dir = supervisor.cfg.root / ".tmp" / "aiflow" / "logs"
    log_file, result_file = log_dir / f"{key}-{role}-{stamp}.log", log_dir / f"{key}-{role}-{stamp}.result"
    model, effort = supervisor.execution_model(role, agent, key, model_override)
    argv, stdin_text = agents.build_command(agent, workdir, result_file, prompt, supervisor.cfg.root, model, effort)
    return argv, stdin_text, log_file, result_file, model, effort


def heartbeat(supervisor: Any, role: str, key: str, agent: str, workdir: Path, branch: str,
              repo: Any, slot_owner: str | None, session_owner: str | None,
              slot_seconds: int, log_message: Any) -> Any:
    pushed_at, last_ok = [time.monotonic()], [time.monotonic()]

    def beat() -> bool:
        if role == "developer" and time.monotonic() - pushed_at[0] > 120:
            pushed_at[0] = time.monotonic()
            try:  # keep the branch on origin so another machine can continue after a crash
                repo.push(workdir, branch)
            except GitError:
                pass
        try:
            supervisor.redis.set(f"hb:{supervisor.cfg.machine}:{supervisor.cfg.worker_label}", str(int(time.time())), 300)
            if slot_owner:
                supervisor.redis.renew(f"qaslot:{repo.name}", slot_owner, slot_seconds)
            session_alive = (not session_owner or supervisor.redis.renew_session(
                supervisor.cfg.machine, agent, session_owner, supervisor.cfg.lease_seconds))
            alive = supervisor.redis.renew(f"lease:{key}", supervisor.cfg.owner,
                                           supervisor.cfg.lease_seconds) and session_alive
        except RedisError as error:
            silent = time.monotonic() - last_ok[0]
            log_message(f"{key}: redis unreachable for {int(silent)}s during {role}: {error}")
            return silent < supervisor.cfg.lease_seconds * 0.75
        last_ok[0] = time.monotonic()
        return alive

    return beat


def finish(supervisor: Any, role: str, agent: str, key: str, repo: Any, workdir: Path,
           log_file: Path, result_file: Path, model: str, effort: str, began: float,
           code: int | None, why: str, log_message: Any) -> tuple[str, str, tuple[str, str] | None, bool]:
    used = usage.report(agent, log_file, ticket=key, role=role, model=model, effort=effort, why=why,
                        code=code, minutes=round((time.monotonic() - began) / 60, 2))
    supervisor.log_usage(key, role, agent, (time.monotonic() - began) / 60, why, code, used)
    text = agents.final_text(agent, log_file, result_file)
    found = agents.verdict(text)
    supervisor.remember_lessons(key, repo, agent, text)
    quota = (code != 0 or found is None) and agents.looks_like_quota(log_file, text, code)
    log_message(f"{key} {role} {agent}: exit={code} why={why} verdict={found} quota={quota}")
    events.emit("ai.run", ticket=key, role=role, agent=agent, model=model, effort=effort,
                minutes=round((time.monotonic() - began) / 60, 2), why=why, code=code,
                verdict=found[0] if found else "", detail=(found[1] if found else "")[:300],
                quota=quota, log_file=str(log_file))
    return why, text, found, quota
