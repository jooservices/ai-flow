"""The supervisor: pick up Jira tickets and drive them through developer, reviewer and QA AIs."""
from __future__ import annotations

import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import agents, runner
from .config import PROJECT_DIR, Settings
from .gitops import GitError, Repo
from .jira import Jira, JiraError
from .lock import Redis

STAGES = {"To Do": "developer", "In Progress": "developer", "In Review": "reviewer", "In QA": "qa"}
DAY = 86400


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}", flush=True)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def ticket_field(description: str, name: str, default: str) -> str:
    match = re.search(rf"\b{name}:\s*([\w./-]+)", description or "")
    return match.group(1) if match else default


class Supervisor:
    def __init__(self, cfg: Settings, jira: Jira, redis: Redis) -> None:
        self.cfg, self.jira, self.redis = cfg, jira, redis
        self.stop = False

    # ---- selection -------------------------------------------------------------------------------------------
    def candidates(self) -> list[dict[str, Any]]:
        projects = ", ".join(self.cfg.projects)
        statuses = ", ".join(f'"{s}"' for s in STAGES)
        jql = f"project in ({projects}) AND status in ({statuses}) AND issuetype != Epic ORDER BY created ASC"
        return self.jira.search(jql)

    def eligible(self, issue: dict[str, Any]) -> bool:
        key, status = issue["key"], issue["status"]
        if status not in STAGES:  # the JQL index can lag behind a transition we just made
            return False
        if "aiflow-hold" in issue.get("labels", []) or self.redis.get(f"lease:{key}"):
            return False
        if status == "To Do":
            return all(b["status"] == "Done" for b in self.jira.blockers(key))
        if status == "In Progress":
            age = (datetime.now(timezone.utc) - parse_time(issue["updated"])).total_seconds()
            return age >= self.cfg.orphan_grace_seconds
        return True

    def cooling(self, agent: str) -> bool:
        return self.redis.get(f"cooldown:{agent}") is not None

    def pick_agent(self, role: str, key: str, field_agent: str | None) -> str | None:
        installed = agents.available(self.cfg.agent_order)
        present = [a for a in installed if not self.cooling(a)]
        if role == "developer":
            if field_agent in present:
                return field_agent
            if field_agent in installed or not field_agent:
                return present[0] if present else None
            return None  # the field names an AI this machine does not have; another machine may take it
        dev = self.redis.get(f"dev:{key}") or field_agent or ""
        if role == "reviewer":
            return agents.choose_other(dev, present)
        reviewer = self.redis.get(f"reviewer:{key}")
        return agents.choose_other(dev, present, avoid=(reviewer,) if reviewer else ())

    # ---- main loop -------------------------------------------------------------------------------------------
    def tick(self) -> bool:
        for issue in self.candidates():
            try:
                if not self.eligible(issue):
                    continue
                view = self.jira.view(issue["key"])
                role = STAGES[issue["status"]]
                agent = self.pick_agent(role, issue["key"], view.get("agent"))
                if agent is None:
                    continue
                if not self.redis.acquire(f"lease:{issue['key']}", self.cfg.owner, self.cfg.lease_seconds):
                    continue
            except (JiraError, RuntimeError) as error:
                log(f"skip {issue.get('key')}: {error}")
                continue
            try:
                self.process(issue, view, role, agent)
            finally:
                self.redis.release(f"lease:{issue['key']}", self.cfg.owner)
            return True
        return False

    def run_forever(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))
        log(f"supervisor {self.cfg.owner} watching {','.join(self.cfg.projects)}")
        while not self.stop:
            try:
                self.redis.set(f"hb:{self.cfg.machine}:{self.cfg.slot}", str(int(time.time())), 300)
                worked = self.tick()
            except Exception as error:  # keep the supervisor alive; the lease and Jira state stay consistent
                log(f"tick failed: {type(error).__name__}: {error}")
                worked = False
            if not worked:
                for _ in range(self.cfg.poll_seconds):
                    if self.stop:
                        break
                    time.sleep(1)

    # ---- one ticket ------------------------------------------------------------------------------------------
    def process(self, issue: dict[str, Any], view: dict[str, Any], role: str, agent: str) -> None:
        key = issue["key"]
        description = view.get("description", "")
        repo = Repo(self.cfg.root, ticket_field(description, "Repo", self.cfg.default_repo))
        base = ticket_field(description, "Base", self.cfg.default_base)
        log(f"{key} [{issue['status']}] {role} -> {agent}")
        if not repo.exists():
            self.block(key, f"Repository `{repo.name}` was not found under JOOservices/projects.")
            return
        home = self.redis.get(f"home:{key}")
        if role != "developer" and home and home != self.cfg.machine and not repo.has_remote():
            log(f"{key}: worktree lives on {home} and the repo has no remote; leaving it")
            return
        if role == "developer":
            self.develop(key, issue, view, repo, base, agent)
        else:
            self.verify(key, role, view, repo, base, agent)

    def attempts(self, key: str, role: str) -> int:
        return int(self.redis.get(f"attempts:{key}:{role}") or 0)

    def block(self, key: str, reason: str) -> None:
        self.jira.comment(key, f"AI Flow: blocked on {self.cfg.machine}. {reason}")
        self.jira.transition(key, "Blocked")

    def execute(self, role: str, agent: str, key: str, view: dict[str, Any], workdir: Path, branch: str, base: str,
                repo: Repo) -> tuple[str, str, tuple[str, str] | None, bool]:
        """Run one AI. Returns (why, final text, verdict, quota)."""
        description = view.get("description", "")
        try:
            comments = "\n".join(f"- {c.get('author', '?')} {c.get('created', '')}: {str(c.get('body', ''))[:1500]}"
                                 for c in self.jira.comments(key, 6)) or "(none)"
        except (JiraError, ValueError, TypeError):
            comments = "(unavailable)"
        values = dict(machine=self.cfg.machine, key=key, summary=view.get("summary", ""), role=role, agent=agent, repo=repo.name,
                      worktree=str(workdir), branch=branch, base=base, root=str(self.cfg.root), description=description, comments=comments)
        prompt = "\n\n".join((PROJECT_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8") for name in ("common", role))
        for name, value in values.items():
            prompt = prompt.replace("{" + name + "}", str(value))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_dir = self.cfg.root / ".tmp" / "aiflow" / "logs"
        log_file, result_file = log_dir / f"{key}-{role}-{stamp}.log", log_dir / f"{key}-{role}-{stamp}.result"
        argv, stdin_text = agents.build_command(agent, workdir, result_file, prompt, self.cfg.root)
        code, why = runner.run(
            argv, stdin_text, workdir, log_file, agents.child_env(),
            idle_seconds=self.cfg.idle_minutes * 60, max_seconds=self.cfg.max_minutes * 60, heartbeat_seconds=self.cfg.heartbeat_seconds,
            heartbeat=lambda: self.redis.renew(f"lease:{key}", self.cfg.owner, self.cfg.lease_seconds))
        text = agents.final_text(agent, log_file, result_file)
        found = agents.verdict(text)
        quota = (code != 0 or found is None) and agents.looks_like_quota(log_file, text)
        log(f"{key} {role} {agent}: exit={code} why={why} verdict={found} quota={quota}")
        return why, text, found, quota

    def quota_hit(self, key: str, agent: str, role: str) -> None:
        self.redis.set(f"cooldown:{agent}", "1", self.cfg.quota_cooldown_minutes * 60)
        self.jira.comment(key, f"AI Flow: {agent} hit its usage limit on {self.cfg.machine} during {role}. "
                               f"Cooling it down for {self.cfg.quota_cooldown_minutes} min; the ticket will be reassigned to another AI "
                               f"and continues from the branch and the hand-over comments.")

    def failed(self, key: str, role: str, agent: str, why: str, text: str, status_hint: str) -> None:
        count = self.redis.incr(f"attempts:{key}:{role}", 7 * DAY)
        self.jira.comment(key, f"AI Flow: {agent} on {self.cfg.machine} ended the {role} run without a result ({why}); "
                               f"attempt {count}/{self.cfg.max_attempts}. Last output: {text[-600:]!r}")
        if count >= self.cfg.max_attempts:
            self.block(key, f"{self.cfg.max_attempts} failed {role} attempts; needs the owner.")

    def develop(self, key: str, issue: dict[str, Any], view: dict[str, Any], repo: Repo, base: str, agent: str) -> None:
        if self.attempts(key, "developer") >= self.cfg.max_attempts:
            self.block(key, "Too many failed developer attempts.")
            return
        if issue["status"] == "To Do" and not self.jira.transition(key, "In Progress"):
            log(f"{key}: could not start (someone else did?)")
            return
        if view.get("agent") != agent:
            self.jira.set_agent(key, agent)
        self.redis.set(f"dev:{key}", agent, 30 * DAY)
        self.redis.set(f"home:{key}", self.cfg.machine, 30 * DAY)
        workdir, branch = repo.ensure_worktree(key, view.get("summary", key), base)
        self.jira.comment(key, f"AI: {agent} on {self.cfg.machine}   Branch: {branch}   Worktree: {workdir}\n"
                               f"Now: starting developer run (resumes from existing commits if any).")
        why, text, found, quota = self.execute("developer", agent, key, view, workdir, branch, base, repo)
        if why == "lost-lease":
            return
        if quota:
            self.quota_hit(key, agent, "developer")
        elif found and found[0] == "DONE" and repo.ahead(workdir, base) > 0:
            pushed = repo.push(workdir, branch)
            self.jira.comment(key, f"AI Flow: {agent} finished on {self.cfg.machine}. Branch {branch}"
                                   f"{' pushed to origin' if pushed else ' (local only)'}; sending to review.\n\n{text[-1500:]}")
            self.redis.delete(f"attempts:{key}:developer")
            self.jira.transition(key, "In Review")
        elif found and found[0] == "BLOCKED":
            self.block(key, f"{agent} reported: {found[1]}")
        else:
            self.failed(key, "developer", agent, why if why != "ok" else "no DONE result or no commits", text, "In Progress")

    def verify(self, key: str, role: str, view: dict[str, Any], repo: Repo, base: str, agent: str) -> None:
        if self.attempts(key, role) >= self.cfg.max_attempts:
            self.block(key, f"Too many failed {role} attempts.")
            return
        found_tree = repo.find_worktree(key)
        if not found_tree:
            self.block(key, "No worktree for the ticket branch; developer output is missing.")
            return
        workdir, branch = found_tree
        if role == "reviewer":
            self.redis.set(f"reviewer:{key}", agent, 30 * DAY)
        self.jira.comment(key, f"AI Flow: {role} {agent} on {self.cfg.machine} started (developer was {self.redis.get(f'dev:{key}') or '?'}).")
        why, text, found, quota = self.execute(role, agent, key, view, workdir, branch, base, repo)
        if why == "lost-lease":
            return
        if quota:
            self.quota_hit(key, agent, role)
            return
        if not found or found[0] not in ("PASS", "FAIL"):
            self.failed(key, role, agent, why if why != "ok" else "no VERDICT line", text, "")
            return
        word, reason = found
        self.redis.delete(f"attempts:{key}:{role}")
        fail_transition = "Review failed" if role == "reviewer" else "QA failed"
        if word == "FAIL":
            cycles = self.redis.incr(f"cycles:{key}", 14 * DAY)
            self.jira.comment(key, f"AI Flow: {role} {agent} FAILED the ticket: {reason}\n\n{text[-1500:]}")
            if cycles >= self.cfg.max_attempts * 2:
                self.block(key, "Too many review/QA failures; needs the owner.")
            else:
                self.jira.transition(key, fail_transition)
            return
        if role == "reviewer":
            self.jira.comment(key, f"AI Flow: reviewer {agent} PASSED. Sending to QA.\n\n{text[-800:]}")
            self.jira.transition(key, "In QA")
            return
        try:
            merged = repo.merge(key, branch, base)
        except GitError as error:
            self.jira.comment(key, f"AI Flow: QA {agent} passed but the merge into {base} failed: {error}")
            self.jira.transition(key, fail_transition)
            return
        repo.cleanup(key)
        self.jira.comment(key, f"AI Flow: QA {agent} PASSED. Merged into {base} ({merged[:10]}), worktree and branch removed.\n\n{text[-800:]}")
        self.jira.transition(key, "Done")
        for suffix in ("dev", "reviewer", "home", "cycles"):
            self.redis.delete(f"{suffix}:{key}")


def main(argv: list[str] | None = None) -> int:
    from .config import load
    cfg = load()
    supervisor = Supervisor(cfg, Jira(cfg.root), Redis(cfg.redis_container, cfg.redis_db, cfg.redis_prefix, cfg.redis_password))
    (cfg.root / ".tmp").mkdir(exist_ok=True)
    if "--once" in (argv if argv is not None else sys.argv[1:]):
        return 0 if supervisor.tick() else 3
    supervisor.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
