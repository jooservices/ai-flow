"""The supervisor: pick up Jira tickets and drive them through developer, reviewer, the pull-request gate and QA."""
from __future__ import annotations

import json
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import agents, github, runner
from .config import PROJECT_DIR, Settings
from .github import GitHub, GitHubError
from .gitops import GitError, Repo
from .jira import Jira, JiraError
from .lock import Redis, RedisError

STAGES = {"To Do": "developer", "In Progress": "developer", "In Review": "reviewer", "Review passed": "pr", "In QA": "gate",
          "Human review": "human", "Ready to merge": "finalize"}
PR_STAGES = ("In QA", "Human review", "Ready to merge")  # checked on GitHub by the supervisor, no AI to pick
DAY = 86400
SLOT_SECONDS = 600


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}", flush=True)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


REQUIRED_SECTIONS = {"AC": ("ac", "acceptance criteria"), "Scopes": ("scope", "scopes"), "Tests": ("test", "tests")}
EMPTY_NOTES = {"", "none", "n/a", "na", "-", "no", "nothing", "not needed", "not required"}


def sections(text: str) -> dict[str, str]:
    """Markdown sections of a Jira text: lower-case heading (without #, ** and a final colon) -> its body."""
    found: dict[str, list[str]] = {}
    current = ""
    for line in (text or "").splitlines():
        match = re.match(r"^\s*#{1,6}\s*(.*?)\s*$", line)
        if match:
            current = match.group(1).strip("*_ :").lower()
            found.setdefault(current, [])
        elif current:
            found[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in found.items()}


def missing_sections(description: str) -> list[str]:
    """Names of the template sections (AC, Scopes, Tests) that are absent or empty in a ticket description."""
    parsed = sections(description)
    return [label for label, names in REQUIRED_SECTIONS.items() if not any(parsed.get(name, "") for name in names)]


def deployment_needed(comment: str) -> bool:
    """True when a hand-over comment has a Deployment section that says something (migration, deploy, ...)."""
    body = sections(comment).get("deployment", "")
    return body.strip(" .-*").lower() not in EMPTY_NOTES


def ticket_field(description: str, name: str, default: str) -> str:
    match = re.search(rf"\b{name}:\s*([\w./-]+)", description or "")
    return match.group(1) if match else default


def decide(info: dict[str, Any]) -> tuple[str, str]:
    """What the pull-request gate does next. Pure: `info` holds facts only.

    Returns (action, detail) with action one of done, closed, update, rework, block, wait, qa, human, awaiting, merge."""
    if info["state"] == "MERGED":
        return "done", ""
    if info["state"] == "CLOSED":
        return "closed", ""
    if info["behind"]:
        return ("rework", "the branch conflicts with the base") if info["conflicting"] else ("update", "")
    if info["ci"] == "failed":
        return "rework", "CI failed"
    if info["ci"] == "pending":
        if info["ci_waited_minutes"] >= info["ci_wait_minutes"]:
            return "block", f"CI is still running after {int(info['ci_wait_minutes'])} minutes"
        return "wait", "CI is running"
    if info["ci"] == "none" and info["ci_required"]:
        return ("block", "no CI result appeared in time") if info["ci_waited_minutes"] >= info["ci_wait_minutes"] else ("wait", "waiting for CI to start")
    if not info["settled"]:
        return "wait", "waiting for review bots to comment"
    if info["stuck"]:
        return "block", "a human review still requests changes: " + ", ".join(info["stuck"])
    if info["feedback"]:
        return "rework", "feedback to resolve"
    if not info["qa_ok"]:
        return "qa", ""
    if info.get("require_human"):
        return "human", ""
    return ("merge", "") if info["auto_merge"] else ("awaiting", "")


class Supervisor:
    def __init__(self, cfg: Settings, jira: Jira, redis: Redis) -> None:
        self.cfg, self.jira, self.redis = cfg, jira, redis
        self.stop = False
        self._priorities: dict[str, tuple[float, str]] = {}

    # ---- selection -------------------------------------------------------------------------------------------
    def candidates(self) -> list[dict[str, Any]]:
        projects = ", ".join(self.cfg.projects)
        statuses = ", ".join(f'"{s}"' for s in STAGES)
        jql = f"project in ({projects}) AND status in ({statuses}) AND issuetype != Epic ORDER BY created ASC"
        found = self.jira.search(jql)
        return sorted(found, key=lambda issue: "rework" not in issue.get("labels", []))  # stable: rework first, then oldest

    def eligible(self, issue: dict[str, Any]) -> bool:
        key, status = issue["key"], issue["status"]
        if status not in STAGES:  # the JQL index can lag behind a transition we just made
            return False
        if "aiflow-hold" in issue.get("labels", []) or self.redis.get(f"lease:{key}"):
            return False
        if status in ("To Do", "In Progress") and self.at_capacity(key):
            return False
        if status == "To Do":
            return all(b["status"] == "Done" for b in self.jira.blockers(key))
        if status == "In Progress":
            age = (datetime.now(timezone.utc) - parse_time(issue["updated"])).total_seconds()
            return age >= self.cfg.orphan_grace_seconds
        if status in PR_STAGES:
            return not self.redis.get(f"gatepoll:{key}")
        return True

    def at_capacity(self, key: str) -> bool:
        """The project already runs as many tickets as its max_parallel allows (0 = no limit)."""
        project = self.cfg.project(key)
        return bool(project.max_parallel) and len(self.redis.scan(f"lease:{project.jira_key}-*")) >= project.max_parallel

    def cooling(self, agent: str) -> bool:
        return self.redis.get(f"cooldown:{agent}") is not None

    def cooldowns(self, names: list[str]) -> set[str]:
        """The AIs on cooldown, in one round trip (the shared Redis can be far away)."""
        return {n for n, v in zip(names, self.redis.get_many([f"cooldown:{n}" for n in names])) if v is not None}

    def priority(self, key: str) -> str:
        """Jira priority name, read at most once a minute per ticket (the agent choice asks for it several times)."""
        seen = self._priorities.get(key)
        if seen and time.time() - seen[0] < 60:
            return seen[1]
        try:
            value = str(self.jira.view(key).get("priority") or "")
        except (JiraError, ValueError, TypeError):
            return ""
        self._priorities[key] = (time.time(), value)
        return value

    def change_type(self, key: str) -> str:
        """The Jira field Type (Feature, Bug, Hotfix, ...); empty when it is not set or cannot be read."""
        try:
            value = self.jira.read_field(key, self.cfg.type_field)
        except JiraError as error:
            log(f"{key}: cannot read Type: {error}")
            return ""
        return str((value or {}).get("value", ""))

    def role_spec(self, role: str, key: str = "") -> tuple[str, str, str]:
        project = self.cfg.project(key)
        if role == "developer" and key and project.highest_developer and self.priority(key) == "Highest":
            return agents.parse_spec(project.highest_developer)  # a Highest ticket goes to the strongest AI unless the ticket names one
        return agents.parse_spec(project.role(role))

    def ticket_spec(self, key: str) -> tuple[str, str]:
        """(AI, model) from the Jira field AI Model, written `<AI>:<Model>`; ('', '') when empty or not understood."""
        try:
            value = self.jira.read_field(key, self.cfg.model_field)
        except JiraError as error:
            log(f"{key}: cannot read AI Model: {error}")
            return "", ""
        agent, _, model = str((value or {}).get("value", "")).partition(":")
        return (agent, model) if agent in agents.NAMES and model else ("", "")

    def model_for(self, role: str, agent: str, key: str = "") -> tuple[str, str]:
        name, model, effort = self.role_spec(role, key)
        chosen = self.state(key, "devmodel") if key and role == "developer" else ""
        if chosen:
            chosen_agent, _, chosen_model = chosen.partition(":")
            if chosen_agent == agent:
                return chosen_model, effort if name == agent else ""
        if name == agent:
            return model, effort
        return (self.cfg.opencode_model, "") if agent == "OpenCode" else ("", "")

    def eligible_agents(self, role: str, key: str, field_agent: str | None, pool: list[str]) -> list[str]:
        """The AIs of `pool` that may take `role` now, best first. The reviewer and QA must differ from the developer."""
        cool = self.cooldowns(pool)
        # AIs in explicit_only (OpenCode, for very easy tasks) work only when the ticket's Agent field names them as developer.
        present = [a for a in pool if a not in cool and (a not in self.cfg.explicit_only or (role == "developer" and a == field_agent))]
        preferred = self.role_spec(role, key)[0]
        if role == "developer":
            if field_agent in present:
                return [field_agent]  # type: ignore[list-item]
            if field_agent and field_agent not in pool:
                return []  # the field names an AI nobody in this pool has
            return sorted(present, key=lambda a: a != preferred)  # empty field, or its AI is cooling: any other AI
        dev = self.redis.get(f"dev:{key}") or field_agent or ""
        reviewer = self.redis.get(f"reviewer:{key}")
        avoid = (reviewer,) if role == "qa" and reviewer else ()
        if preferred in present and preferred != dev and preferred not in avoid:
            return [preferred]
        other = agents.choose_other(dev, present, avoid=avoid)
        return [other] if other else []

    def pick_agent(self, role: str, key: str, field_agent: str | None) -> str | None:
        found = self.eligible_agents(role, key, field_agent, agents.available(self.cfg.agent_order))
        return found[0] if found else None

    def publish_ais(self) -> None:
        """Tell the other machines which AIs this one has (the entry expires when the machine stops beating)."""
        self.redis.set(f"ais:{self.cfg.machine}", ",".join(agents.available(self.cfg.agent_order)), 600)

    def online_pool(self) -> list[str]:
        """AIs installed on this machine or on any machine that published recently."""
        pool = list(agents.available(self.cfg.agent_order))
        for name in self.redis.scan("ais:*"):
            for ai in (self.redis.get(name) or "").split(","):
                if ai and ai not in pool:
                    pool.append(ai)
        return sorted(pool, key=lambda a: self.cfg.agent_order.index(a) if a in self.cfg.agent_order else 99)

    def unserved(self, key: str, role: str, field_agent: str | None) -> None:
        """No AI on this machine can take the step. Another online machine may; if nobody can for long, block with the reason."""
        pool = self.online_pool()
        if self.eligible_agents(role, key, field_agent, pool):
            return  # a supervisor on another machine will take it
        self.note_waiting(key, role)
        since = self.redis.get(f"waiting:{key}:{role}")
        if since is None or time.time() - int(since) < self.cfg.quota_block_minutes * 60:
            return
        cooling = sorted(self.cooldowns(pool))
        if cooling:
            self.block(key, f"No AI can take the {role} step: {', '.join(cooling)} out of quota (cooling down), the others are offline "
                            f"or not allowed for this step. The supervisor re-checks and continues when one is back.", kind="Quota")
        else:
            self.block(key, f"No online machine has an AI for the {role} step (online AIs: {', '.join(pool) or 'none'}). "
                            f"The supervisor continues when one comes online.", kind="Offline")

    def note_waiting(self, key: str, role: str) -> None:
        """Say so on the ticket when it has waited too long because no suitable AI is available."""
        name = f"waiting:{key}:{role}"
        since = self.redis.get(name)
        if since is None:
            self.redis.set(name, str(int(time.time())), 7 * DAY)
            return
        if time.time() - int(since) > self.cfg.wait_note_minutes * 60 and not self.redis.get(f"waitnote:{key}:{role}"):
            self.redis.set(f"waitnote:{key}:{role}", "1", 2 * 3600)
            self.jira.comment(key, f"AI Flow: waiting more than {self.cfg.wait_note_minutes} min for an AI to run the {role} step "
                                   f"(the {role} must differ from the developer, and the other AIs are cooling down or not installed on this machine).")

    # ---- main loop -------------------------------------------------------------------------------------------
    def tick(self) -> bool:
        for issue in self.candidates():
            try:
                if not self.eligible(issue):
                    continue
                view = self.jira.view(issue["key"], full=True)
                role = STAGES[issue["status"]]
                agent = None
                if role == "developer":
                    spec_agent, spec_model = self.ticket_spec(issue["key"])
                    if spec_agent:
                        view = {**view, "agent": spec_agent, "spec_model": spec_model, "agent_field": view.get("agent")}
                if role in ("developer", "reviewer"):
                    agent = self.pick_agent(role, issue["key"], view.get("agent"))
                    if agent is None:
                        self.unserved(issue["key"], role, view.get("agent"))
                        continue
                if not self.redis.acquire(f"lease:{issue['key']}", self.cfg.owner, self.cfg.lease_seconds):
                    continue
            except (JiraError, RuntimeError) as error:
                log(f"skip {issue.get('key')}: {error}")
                continue
            try:
                worked = self.process(issue, view, role, agent)
            finally:
                try:
                    self.redis.release(f"lease:{issue['key']}", self.cfg.owner)
                except RedisError as error:
                    log(f"{issue['key']}: could not release the lease ({error}); it expires by itself")
            if worked:
                return True
        return False

    def run_forever(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))
        log(f"supervisor {self.cfg.owner} watching {','.join(self.cfg.projects)}")
        started = int(time.time())
        while not self.stop:
            if self.should_exit(started):
                break
            try:
                self.redis.set(f"hb:{self.cfg.machine}:{self.cfg.worker_label}", str(int(time.time())), 300)
                self.publish_ais()
                self.recheck_blocked()
                worked = self.tick()
            except Exception as error:  # keep the supervisor alive; the lease and Jira state stay consistent
                log(f"tick failed: {type(error).__name__}: {error}")
                worked = False
            if not worked:
                for _ in range(self.cfg.poll_seconds):
                    if self.stop:
                        break
                    time.sleep(1)

    def should_exit(self, started: int) -> bool:
        """Graceful exit between two tickets: after `aiflow-work restart` or after the maximum run time. systemd
        starts the worker again, so it runs the newest code and configuration."""
        asked = self.redis.get("restart")
        if asked is not None and int(asked) >= started:
            log("restart requested; leaving after the current ticket step")
            return True
        limit = self.cfg.worker_max_minutes
        if limit > 0 and time.time() - started > limit * 60:
            log(f"worker ran for {limit} minutes; leaving so systemd starts a fresh one")
            return True
        return False

    # ---- one ticket ------------------------------------------------------------------------------------------
    def process(self, issue: dict[str, Any], view: dict[str, Any], role: str, agent: str | None) -> bool:
        """Returns True when something was done (so the loop does not sleep), False when the ticket only waits."""
        key = issue["key"]
        description = view.get("description", "")
        project = self.cfg.project(key)
        if issue["status"] == "To Do" and self.incomplete(key, view):
            return True
        repo = Repo(self.cfg.root, ticket_field(description, "Repo", project.repo), project.company)
        change = self.change_type(key)
        base = project.hotfix_base if change == "Hotfix" else ticket_field(description, "Base", project.base)
        if role in ("developer", "reviewer"):
            log(f"{key} [{issue['status']}] {role} -> {agent}")
        if not repo.exists():
            self.block(key, f"Repository `{repo.name}` was not found under JOOservices/projects.", kind="Config")
            return True
        if not repo.has_remote():
            self.block(key, f"Repository `{repo.name}` has no remote; the flow needs GitHub pull requests.", kind="Config")
            return True
        self.redis.set(f"repo:{key}", repo.name, 30 * DAY)
        home = self.redis.get(f"home:{key}")
        if role == "reviewer" and home and home != self.cfg.machine and not repo.remote_branch(key):
            log(f"{key}: worktree lives on {home} and the branch was never pushed; leaving it")
            return False
        if role == "developer":
            return self.develop(key, issue, view, repo, base, agent or "")
        if role == "reviewer":
            return self.review(key, view, repo, base, agent or "")
        if role == "pr":
            return self.pr_step(key, view, repo, base)
        if role == "human":
            return self.human_wait(key, repo, base)
        if role == "finalize":
            return self.finalize(key, repo, base)
        return self.gate(key, view, repo, base)

    def incomplete(self, key: str, view: dict[str, Any]) -> bool:
        """A ticket in To Do must carry AC, Scopes and Tests; otherwise it goes back to Backlog with a note and no AI starts."""
        missing = missing_sections(view.get("description", ""))
        if not missing:
            return False
        self.jira.comment(key, "AI Flow: this ticket cannot start. The description misses the section(s): " + ", ".join(f"## {m}" for m in missing)
                          + ". Add them (template: README, \"Ticket template\") and move the ticket to To Do again.")
        if not self.jira.transition(key, "Backlog"):
            self.block(key, "The description misses " + ", ".join(missing) + ".", kind="Unclear")
        return True

    def attempts(self, key: str, role: str) -> int:
        return int(self.redis.get(f"attempts:{key}:{role}") or 0)

    def block(self, key: str, reason: str, kind: str = "Other") -> None:
        """Move the ticket to Blocked, say why on the ticket and in the field Blocked reason, and remember where it came from."""
        self.release_slot(key)
        for name in ("attempts:%s:developer", "attempts:%s:reviewer", "attempts:%s:qa", "cycles:%s"):
            self.redis.delete(name % key)  # the owner who unblocks the ticket gets a fresh start, not an instant second block
        try:
            status = str(self.jira.view(key).get("status", ""))
            self.redis.set(f"blocked:{key}", json.dumps({"status": status, "kind": kind}), 30 * DAY)
            self.jira.set_field(key, self.cfg.blocked_reason_field, f"{kind}: {reason}"[:250])
        except (JiraError, RedisError) as error:
            log(f"{key}: could not record the block reason: {error}")
        self.jira.comment(key, f"AI Flow: blocked on {self.cfg.machine} ({kind}). {reason}")
        self.jira.transition(key, "Blocked")

    def recheck_blocked(self) -> None:
        """Tickets blocked for lack of an AI (Quota, Offline) continue by themselves when an AI is available again."""
        if not self.redis.acquire("recheck", self.cfg.owner, self.cfg.recheck_seconds):
            return
        projects = ", ".join(self.cfg.projects)
        for issue in self.jira.search(f"project in ({projects}) AND status = Blocked AND issuetype != Epic ORDER BY created ASC"):
            key = issue["key"]
            raw = self.redis.get(f"blocked:{key}")
            if not raw or "aiflow-hold" in issue.get("labels", []):
                continue
            info = json.loads(raw)
            role = {"To Do": "developer", "In Progress": "developer", "In Review": "reviewer", "In QA": "qa"}.get(info["status"])
            if info["kind"] not in ("Quota", "Offline") or not role:
                continue
            view = self.jira.view(key)
            if not self.eligible_agents(role, key, view.get("agent"), self.online_pool()):
                continue
            if self.jira.transition(key, info["status"]):
                self.jira.set_field(key, self.cfg.blocked_reason_field, None)
                self.jira.comment(key, f"AI Flow: an AI for the {role} step is available again; the ticket returns to {info['status']}.")
                for name in (f"blocked:{key}", f"waiting:{key}:{role}", f"waitnote:{key}:{role}"):
                    self.redis.delete(name)

    def clear_block(self, key: str) -> None:
        """The owner moved a blocked ticket back to To Do himself: forget why it was blocked (the answer is in the comments)."""
        if self.redis.get(f"blocked:{key}") is None:
            return
        self.redis.delete(f"blocked:{key}")
        try:
            self.jira.set_field(key, self.cfg.blocked_reason_field, None)
        except JiraError as error:
            log(f"{key}: cannot clear Blocked reason: {error}")

    def label_workspace(self, key: str) -> None:
        try:
            self.jira.set_workspace(key, self.cfg.workspace_field, self.cfg.workspace_name)
        except JiraError as error:
            log(f"{key}: cannot set Workspace: {error}")

    # ---- running an AI ---------------------------------------------------------------------------------------
    def state(self, key: str, name: str, default: str = "") -> str:
        return self.redis.get(f"{name}:{key}") or default

    def rework_text(self, key: str) -> str:
        text = self.state(key, "rework")
        return text or "(none: this is the first pass)"

    def pr_text(self, key: str) -> str:
        number = self.state(key, "pr")
        return f"Pull request #{number} (see the ticket comments for the link)." if number else "(not opened yet)"

    def replies_file(self, key: str) -> Path:
        path = self.cfg.root / ".tmp" / "aiflow" / "replies" / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def execute(self, role: str, agent: str, key: str, view: dict[str, Any], workdir: Path, branch: str, base: str,
                repo: Repo, slot_owner: str | None = None) -> tuple[str, str, tuple[str, str] | None, bool]:
        """Run one AI. Returns (why, final text, verdict, quota)."""
        description = view.get("description", "")
        try:
            comments = "\n".join(f"- {c.get('author', '?')} {c.get('created', '')}: {str(c.get('body', ''))[:1500]}"
                                 for c in self.jira.comments(key, 6)) or "(none)"
        except (JiraError, ValueError, TypeError):
            comments = "(unavailable)"
        values = dict(machine=self.cfg.machine, key=key, summary=view.get("summary", ""), role=role, agent=agent, repo=repo.name,
                      worktree=str(workdir), branch=branch, base=base, root=str(self.cfg.root), description=description, comments=comments,
                      rework=self.rework_text(key), pr=self.pr_text(key), replies_file=str(self.replies_file(key)),
                      type=self.change_type(key) or str(view.get("type", "")), parent=view.get("parent") or "none")
        prompt = "\n\n".join((PROJECT_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8") for name in ("common", role))
        for name, value in values.items():
            prompt = prompt.replace("{" + name + "}", str(value))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_dir = self.cfg.root / ".tmp" / "aiflow" / "logs"
        log_file, result_file = log_dir / f"{key}-{role}-{stamp}.log", log_dir / f"{key}-{role}-{stamp}.result"
        model, effort = self.model_for(role, agent, key)
        argv, stdin_text = agents.build_command(agent, workdir, result_file, prompt, self.cfg.root, model, effort)
        pushed_at = [time.monotonic()]

        last_ok = [time.monotonic()]

        def beat() -> bool:
            if role == "developer" and time.monotonic() - pushed_at[0] > 120:
                pushed_at[0] = time.monotonic()
                try:  # keep the branch on origin so another machine can continue after a crash
                    repo.push(workdir, branch)
                except GitError:
                    pass
            try:
                self.redis.set(f"hb:{self.cfg.machine}:{self.cfg.worker_label}", str(int(time.time())), 300)
                if slot_owner:
                    self.redis.renew(f"qaslot:{repo.name}", slot_owner, SLOT_SECONDS)
                alive = self.redis.renew(f"lease:{key}", self.cfg.owner, self.cfg.lease_seconds)
            except RedisError as error:
                # A short outage is tolerated, but the lease expires lease_seconds after the last renewal and another
                # supervisor may then take the ticket, so stop the AI before that.
                silent = time.monotonic() - last_ok[0]
                log(f"{key}: redis unreachable for {int(silent)}s during {role}: {error}")
                return silent < self.cfg.lease_seconds * 0.75
            last_ok[0] = time.monotonic()
            return alive

        code, why = runner.run(
            argv, stdin_text, workdir, log_file, agents.run_env(agent, workdir, self.cfg.root),
            idle_seconds=self.cfg.idle_minutes * 60, max_seconds=self.cfg.max_minutes * 60, heartbeat_seconds=self.cfg.heartbeat_seconds,
            heartbeat=beat)
        text = agents.final_text(agent, log_file, result_file)
        found = agents.verdict(text)
        quota = (code != 0 or found is None) and agents.looks_like_quota(log_file, text, code)
        log(f"{key} {role} {agent}: exit={code} why={why} verdict={found} quota={quota}")
        return why, text, found, quota

    def quota_hit(self, key: str, agent: str, role: str) -> None:
        self.redis.set(f"cooldown:{agent}", "1", self.cfg.quota_cooldown_minutes * 60)
        self.jira.comment(key, f"AI Flow: {agent} is not usable on {self.cfg.machine} during {role} (usage limit, empty balance or sign-in problem). "
                               f"Cooling it down for {self.cfg.quota_cooldown_minutes} min; the ticket will be reassigned to another AI "
                               f"and continues from the branch and the hand-over comments.")

    def failed(self, key: str, role: str, agent: str, why: str, text: str) -> None:
        count = self.redis.incr(f"attempts:{key}:{role}", 7 * DAY)
        self.jira.comment(key, f"AI Flow: {agent} on {self.cfg.machine} ended the {role} run without a usable result ({why}); "
                               f"attempt {count}/{self.cfg.max_attempts}. Last output: {text[-600:]!r}")
        if count >= self.cfg.max_attempts:
            self.block(key, f"{self.cfg.max_attempts} failed {role} attempts; needs the owner.", kind="Attempts")

    # ---- developer -------------------------------------------------------------------------------------------
    def develop(self, key: str, issue: dict[str, Any], view: dict[str, Any], repo: Repo, base: str, agent: str) -> bool:
        if self.attempts(key, "developer") >= self.cfg.max_attempts:
            self.block(key, "Too many failed developer attempts.", kind="Attempts")
            return True
        if issue["status"] == "To Do" and not self.jira.transition(key, "In Progress"):
            log(f"{key}: could not start (someone else did?)")
            return False
        if issue["status"] == "To Do":
            self.clear_block(key)
        if view.get("agent_field", view.get("agent")) != agent:  # keep Agent (AI) equal to the AI that really works (AI Model may differ)
            self.jira.set_agent(key, agent)
        if view.get("spec_model") and view.get("agent") == agent:
            self.redis.set(f"devmodel:{key}", f"{agent}:{view['spec_model']}", 30 * DAY)
        else:
            self.redis.delete(f"devmodel:{key}")  # another AI took over: it runs with its own default model
        self.label_workspace(key)
        if "rework" in issue.get("labels", []):
            self.jira.remove_label(key, "rework")
        previous = self.redis.get(f"dev:{key}")
        self.redis.set(f"dev:{key}", agent, 30 * DAY)
        self.redis.set(f"home:{key}", self.cfg.machine, 30 * DAY)
        try:
            workdir, branch = repo.ensure_worktree(key, view.get("summary", key), base)
            repo.adopt_remote(workdir, branch)
        except GitError as error:
            self.failed(key, "developer", agent, "worktree", str(error))
            return True
        takeover = ""
        if issue["status"] == "In Progress" and previous:
            reason = "hit its usage limit" if self.cooling(previous) else "stopped (died, lost its lease or showed no activity)"
            takeover = f" Taking over from {previous}, which {reason}; continuing from the branch and the comments above."
        kind = "rework" if self.state(key, "rework") else "developer"
        self.jira.comment(key, f"AI: {agent} on {self.cfg.machine}   Branch: {branch}   Worktree: {workdir}\n"
                               f"Now: starting {kind} run (resumes from existing commits if any).{takeover}")
        self.replies_file(key).unlink(missing_ok=True)
        why, text, found, quota = self.execute("developer", agent, key, view, workdir, branch, base, repo)
        if why == "lost-lease":
            return True
        if quota:
            self.quota_hit(key, agent, "developer")
        elif found and found[0] == "DONE" and repo.ahead(workdir, base) > 0:
            self.finish_development(key, view, repo, base, agent, workdir, branch, text)
        elif found and found[0] == "BLOCKED":
            self.block(key, f"{agent} reported: {found[1]}", kind="Unclear")
        else:
            self.failed(key, "developer", agent, why if why != "ok" else "no DONE result or no commits", text)
        return True

    def finish_development(self, key: str, view: dict[str, Any], repo: Repo, base: str, agent: str, workdir: Path, branch: str, text: str) -> None:
        bad = [s for s in repo.commit_subjects(workdir, base) if not github.valid_title(s)]
        if bad:
            self.failed(key, "developer", agent, "commit messages break Conventional Commits "
                        "(type(scope): Subject, uppercase first letter, no final period, 10-120 chars): " + "; ".join(bad[:4]), text)
            return
        answers = self.read_replies(key)
        if isinstance(answers, str):
            self.failed(key, "developer", agent, answers, text)
            return
        title = agents.pr_title(text) or ""
        if not github.valid_title(title):
            title = github.fallback_title(key, view.get("summary", key), str(view.get("type", "")), self.change_type(key))
        self.redis.set(f"prtitle:{key}", github.with_key(title, key), 30 * DAY)
        try:
            repo.push(workdir, branch, lease=True)
        except GitError as error:
            self.failed(key, "developer", agent, f"push failed: {error}", text)
            return
        self.redis.set(f"pushed:{key}", str(int(time.time())), 30 * DAY)
        self.require_human_for_deployment(key)
        for name in ("attempts:%s:developer", "qa:%s"):
            self.redis.delete(name % key)
        if answers and self.post_replies(key, repo, answers):
            return
        if self.state(key, "humanok"):  # a human already approved: no review, no QA again, only CI
            self.jira.comment(key, f"AI Flow: {agent} finished the rework on {self.cfg.machine}. Branch {branch} pushed; the human approved "
                                   f"this ticket before, so it goes back to Ready to merge (CI must pass).\n\n{text[-1000:]}")
            self.jira.transition(key, "Ready to merge")
            return
        self.jira.comment(key, f"AI Flow: {agent} finished on {self.cfg.machine}. Branch {branch} pushed to origin; sending to review.\n\n{text[-1500:]}")
        self.jira.transition(key, "In Review")

    def require_human_for_deployment(self, key: str) -> None:
        """A hand-over comment with a Deployment section (migration, deploy) needs a human before the merge."""
        try:
            latest = self.jira.comments(key, 3)
            if any(deployment_needed(str(c.get("body", ""))) for c in latest if "## Deployment" in str(c.get("body", ""))):
                self.jira.set_field(key, self.cfg.require_human_field, {"value": "Yes"})
                log(f"{key}: Deployment section found; Require human = Yes")
        except (JiraError, ValueError, TypeError) as error:
            log(f"{key}: cannot check the Deployment section: {error}")

    def read_replies(self, key: str) -> list[tuple[dict[str, Any], dict[str, Any]]] | str:
        """The developer's answers paired with the feedback items, or a problem text when an item has no valid answer."""
        raw = self.state(key, "feedback")
        if not raw:
            return []
        wanted = json.loads(raw)
        path = self.replies_file(key)
        try:
            replies = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError, KeyError, TypeError):
            return f"feedback items need answers in {path} (a JSON list of id/action/reply)"
        missing = [i["id"] for i in wanted if replies.get(i["id"], {}).get("action") not in ("fixed", "rejected")
                   or not str(replies[i["id"]].get("reply", "")).strip()]
        if missing:
            return "no valid answer for feedback item(s): " + ", ".join(missing)
        return [(item, replies[item["id"]]) for item in wanted]

    def post_replies(self, key: str, repo: Repo, answers: list[tuple[dict[str, Any], dict[str, Any]]]) -> bool:
        """Answer every item on GitHub and resolve threads. Returns True when the ticket was blocked (a human's request was rejected)."""
        number = int(self.state(key, "pr") or 0)
        gh = GitHub(repo.github_slug())
        rejected_human = []
        for item, answer in answers:
            if answer["action"] == "rejected" and not item["bot"]:
                rejected_human.append(item)
            try:
                gh.reply(item["id"], number, f"{'Fixed' if answer['action'] == 'fixed' else 'Not applied'}: {answer['reply']}")
                if item["kind"] == "thread" and (answer["action"] == "fixed" or item["bot"]):
                    gh.resolve(item["id"].split(":", 1)[1])
            except GitHubError as error:
                self.jira.comment(key, f"AI Flow: could not answer {item['id']} on GitHub ({error}).")
        self.redis.delete(f"feedback:{key}")
        if rejected_human:
            who = ", ".join(sorted({i["who"] for i in rejected_human}))
            self.block(key, f"The developer rejected feedback from {who}; the owner decides (dismiss the review or ask for the change). "
                            "Reasons are in the replies on the pull request.", kind="Human")
            return True
        return False

    # ---- reviewer --------------------------------------------------------------------------------------------
    def review(self, key: str, view: dict[str, Any], repo: Repo, base: str, agent: str) -> bool:
        if self.attempts(key, "reviewer") >= self.cfg.max_attempts:
            self.block(key, "Too many failed reviewer attempts.", kind="Attempts")
            return True
        if self.attempts(key, "pr") >= self.cfg.max_attempts:  # only reached when the workflow has no Review passed status
            self.block(key, "The pull request could not be opened after several tries.", kind="Attempts")
            return True
        try:
            workdir, branch = repo.ensure_worktree(key, view.get("summary", key), base)
            repo.sync_worktree(workdir, branch)
            repo.fetch(base)
        except GitError as error:
            self.block(key, f"Cannot get the ticket branch on {self.cfg.machine}: {error}", kind="Config")
            return True
        if repo.ahead(workdir, base) == 0:
            self.block(key, "The ticket branch has no commits ahead of the base; developer output is missing.", kind="Other")
            return True
        self.redis.set(f"reviewer:{key}", agent, 30 * DAY)
        self.label_workspace(key)
        self.jira.comment(key, f"AI Flow: reviewer {agent} on {self.cfg.machine} started (developer was {self.redis.get(f'dev:{key}') or '?'}).")
        why, text, found, quota = self.execute("reviewer", agent, key, view, workdir, branch, base, repo)
        if why == "lost-lease":
            return True
        if quota:
            self.quota_hit(key, agent, "reviewer")
            return True
        if not found or found[0] not in ("PASS", "FAIL"):
            self.failed(key, "reviewer", agent, why if why != "ok" else "no VERDICT line", text)
            return True
        word, reason = found
        self.redis.delete(f"attempts:{key}:reviewer")
        if word == "FAIL":
            self.send_back(key, repo, f"reviewer {agent} FAILED the change: {reason}", text[-3000:], [])
            return True
        self.redis.set(f"reviewtext:{key}", text[-800:], 30 * DAY)
        self.jira.comment(key, f"AI Flow: reviewer {agent} PASSED.\n\n{text[-1500:]}")
        if not self.jira.transition(key, "Review passed"):  # a project whose workflow has no such status opens the pull request at once
            self.open_pull_request(key, view, repo, base, branch, agent, text, role="pr")
        return True

    def pr_step(self, key: str, view: dict[str, Any], repo: Repo, base: str) -> bool:
        """Review passed: the supervisor opens the pull request (no AI runs); a failure here never counts against the reviewer."""
        if self.attempts(key, "pr") >= self.cfg.max_attempts:
            self.block(key, "The pull request could not be opened after several tries.", kind="Attempts")
            return True
        try:
            branch = repo.remote_branch_strict(key)
        except GitError as error:
            log(f"{key}: cannot list the remote branches ({error}); trying again")
            return False  # a network problem is not a missing branch
        if not branch:
            self.block(key, "The ticket branch is not on origin; the developer output was never pushed.", kind="Config")
            return True
        self.open_pull_request(key, view, repo, base, branch, self.state(key, "reviewer", "?"), self.state(key, "reviewtext"), role="pr")
        return True

    def pr_body(self, key: str, view: dict[str, Any], repo: Repo, base: str, reviewer: str) -> str:
        """The repository's pull request template filled from the developer's template comment."""
        said: dict[str, str] = {}
        try:
            for comment in self.jira.comments(key, 12):
                found = sections(str(comment.get("body", "")))
                if "what's changed" in found:
                    said = found  # the newest comment that follows the template wins
        except (JiraError, ValueError, TypeError) as error:
            log(f"{key}: cannot read the developer comment for the PR body: {error}")
        developer = self.state(key, "dev", "?")
        done = said.get("what's changed", "").strip() or str(view.get("summary", key))
        tests = (f"- [x] Reviewed by {reviewer} (AI Flow; developed by {developer})\n- [ ] CI green\n- [ ] QA on the pull request head")
        return github.fill_template(repo.pr_template(base), {
            "summary": done, "why": said.get("why changed", "").strip(), "tests": tests, "deployment": said.get("deployment", "").strip(),
            "ticket": key, "footer": github.MARKER})

    def open_pull_request(self, key: str, view: dict[str, Any], repo: Repo, base: str, branch: str, agent: str, text: str, role: str = "reviewer") -> None:
        gh = GitHub(repo.github_slug())
        title = self.state(key, "prtitle") or github.fallback_title(key, view.get("summary", key), str(view.get("type", "")), self.change_type(key))
        try:
            found = gh.find(branch)
            if found is None or found["state"] != "OPEN":
                found = gh.create(branch, base, title, self.pr_body(key, view, repo, base, agent))
        except GitHubError as error:
            self.failed(key, role, agent, f"could not open the pull request: {error}", text)
            return
        self.redis.set(f"pr:{key}", str(found["number"]), 30 * DAY)
        self.redis.set(f"pushed:{key}", self.state(key, "pushed", str(int(time.time()))), 30 * DAY)
        self.redis.set(f"gateentry:{key}", str(int(time.time())), 30 * DAY)
        self.jira.comment(key, f"AI Flow: pull request opened by the supervisor: {found['url']}. Waiting for CI, feedback and QA.")
        self.jira.transition(key, "In QA")

    # ---- send back (rework) ----------------------------------------------------------------------------------
    def release_slot(self, key: str, repo_name: str = "") -> None:
        name = repo_name or self.state(key, "repo")
        if name:
            self.redis.release(f"qaslot:{name}", key)

    def send_back(self, key: str, repo: Repo, headline: str, details: str, items: list[dict[str, Any]]) -> None:
        """Return the ticket to To Do with a reason the next developer run can act on."""
        self.release_slot(key, repo.name)
        cycles = self.redis.incr(f"cycles:{key}", 30 * DAY)
        lines = [f"Rework needed (round {cycles}): {headline}", "", details.strip()]
        if items:
            lines += ["", "Feedback items. Answer each one in the replies file (see the developer instructions):"]
            for item in items:
                lines.append(f"- id `{item['id']}` from {item['who']}{' (bot)' if item['bot'] else ''} at {item['where']}: {item['text'][:800]}")
            self.redis.set(f"feedback:{key}", json.dumps(items), 30 * DAY)
            seen = set(json.loads(self.state(key, "seen", "[]")))
            seen.update(i["id"] for i in items if i["kind"] != "thread")
            self.redis.set(f"seen:{key}", json.dumps(sorted(seen)), 30 * DAY)
        text = "\n".join(lines)
        self.redis.set(f"rework:{key}", text[:12000], 30 * DAY)
        self.redis.delete(f"qa:{key}")
        self.jira.comment(key, f"AI Flow: {text[:6000]}")
        if cycles > self.cfg.max_cycles:
            self.block(key, f"{cycles - 1} rework rounds without success; needs the owner.", kind="Rework")
            return
        self.jira.add_label(key, "rework")
        self.jira.transition(key, "To Do")

    # ---- the pull-request gate (status In QA) ----------------------------------------------------------------
    def gate(self, key: str, view: dict[str, Any], repo: Repo, base: str) -> bool:
        self.redis.set(f"gatepoll:{key}", "1", self.cfg.gate_poll_seconds)
        worked = self._gate(key, view, repo, base)
        if worked:
            self.redis.delete(f"gatepoll:{key}")  # something changed: look again at the next tick
        return worked

    def _gate(self, key: str, view: dict[str, Any], repo: Repo, base: str) -> bool:
        branch = repo.remote_branch(key)
        if not branch:
            self.block(key, "The ticket branch is not on origin, so there is no pull request to check.", kind="Config")
            return True
        gh = GitHub(repo.github_slug())
        try:
            pr = gh.find(branch)
            if pr is None:
                self.block(key, f"No pull request exists for branch {branch}.", kind="Config")
                return True
            info = self.gate_facts(key, repo, gh, base, branch, pr)
        except (GitHubError, GitError, ValueError) as error:
            log(f"{key}: gate check failed: {error}")
            return False
        action, detail = decide(info)
        if action in ("qa", "merge") and not self.take_slot(key, repo):
            self.note_gate(key, "waiting for another pull request of this repository to finish QA and merge")
            return False
        self.redis.set(f"pr:{key}", str(pr["number"]), 30 * DAY)
        if action == "done":
            self.finish(key, repo, base, pr, f"Pull request {pr['url']} is merged into {base}.")
        elif action == "closed":
            repo.cleanup(key, remote=False)
            self.release_slot(key, repo.name)
            self.jira.comment(key, f"AI Flow: pull request {pr['url']} was closed without merging; declining the ticket.")
            self.jira.transition(key, "Declined")
        elif action == "update":
            return self.update_branch(key, gh, pr, base)
        elif action == "rework":
            self.rework_from_gate(key, repo, gh, pr, info, detail)
        elif action == "block":
            self.block(key, f"{detail} ({pr['url']}).", kind="CI" if "CI" in detail else "Human")
        elif action == "wait":
            self.note_gate(key, f"{detail} ({pr['url']})")
            return False
        elif action == "qa":
            return self.run_qa(key, view, repo, base, branch, pr)
        elif action == "human":
            self.to_human_review(key, repo, pr)
        elif action == "awaiting":
            self.release_slot(key, repo.name)
            self.note_gate(key, f"QA passed; auto-merge is off for {repo.name}, waiting for the owner to merge {pr['url']}")
            return False
        else:
            return self.merge(key, repo, gh, pr, base)
        return True

    def gate_facts(self, key: str, repo: Repo, gh: GitHub, base: str, branch: str, pr: dict[str, Any]) -> dict[str, Any]:
        ci, failing = github.checks_state(pr.get("statusCheckRollup"))
        info: dict[str, Any] = dict(state=pr["state"], ci=ci, failing=failing, behind=False, conflicting=False, feedback=[], stuck=[])
        project = self.cfg.project(key)
        required = project.ci_required
        entry = int(self.state(key, "gateentry", str(int(time.time()))))
        pushed = int(self.state(key, "pushed", "0"))
        info.update(ci_required=required, ci_wait_minutes=self.cfg.ci_wait_minutes, ci_waited_minutes=(time.time() - max(entry, pushed)) / 60,
                    settled=time.time() - pushed >= self.cfg.feedback_settle_minutes * 60,
                    qa_ok=self.state(key, "qa") == pr["headRefOid"], auto_merge=project.auto_merge)
        if pr["state"] != "OPEN":
            return info
        info["behind"] = repo.behind(base, branch)
        info["conflicting"] = pr.get("mergeable") == "CONFLICTING"
        seen = set(json.loads(self.state(key, "seen", "[]")))
        found = gh.feedback(pr["number"], seen, self.cfg.ignored_authors)
        info["feedback"], info["stuck"] = found["items"], found["stuck"]
        if found.get("untrusted") and self.redis.acquire(f"untrusted:{key}", self.cfg.owner, 6 * 3600):
            log(f"{key}: ignored feedback from accounts without write access: {', '.join(found['untrusted'])}")
        info["require_human"] = self.human_required(key)
        return info

    def human_required(self, key: str) -> bool:
        try:
            value = self.jira.read_field(key, self.cfg.require_human_field)
        except JiraError as error:
            log(f"{key}: cannot read Require human: {error}")
            return self.cfg.project(key).require_human
        if value:
            return value.get("value") == "Yes"
        return self.cfg.project(key).require_human  # the field is empty: the project decides

    def take_slot(self, key: str, repo: Repo) -> bool:
        """One pull request per repository is in QA/merge at a time, so a merge never makes the next QA stale twice."""
        name = f"qaslot:{repo.name}"
        return self.redis.acquire(name, key, SLOT_SECONDS) or self.redis.renew(name, key, SLOT_SECONDS)

    def note_gate(self, key: str, message: str) -> None:
        marker = f"gatenote:{key}"
        if self.redis.get(marker) != message:
            self.redis.set(marker, message, 30 * DAY)
            self.jira.comment(key, f"AI Flow: {message}.")

    def update_branch(self, key: str, gh: GitHub, pr: dict[str, Any], base: str, qa_again: bool = True) -> bool:
        try:
            gh.update_branch(pr["number"], pr["headRefOid"])
        except GitHubError as error:
            self.jira.comment(key, f"AI Flow: the branch is behind {base} and updating it failed ({error}); the next check retries.")
            return False
        self.redis.delete(f"qa:{key}")
        self.redis.set(f"pushed:{key}", str(int(time.time())), 30 * DAY)
        self.jira.comment(key, f"AI Flow: {base} moved, so {pr['url']} was updated with {base} (clean merge). CI runs again on the new head{' and QA' if qa_again else ' (the human approved it, so QA is not repeated)'}.")
        return True

    def rework_from_gate(self, key: str, repo: Repo, gh: GitHub, pr: dict[str, Any], info: dict[str, Any], detail: str) -> None:
        if detail == "CI failed":
            parts = []
            for check in info["failing"][:5]:
                tail = gh.failure_log(check["link"])
                parts.append(f"- {check['name']}: {check['link']}" + (f"\n```\n{tail}\n```" if tail else ""))
            self.send_back(key, repo, f"CI failed on {pr['url']}", "Failing checks:\n" + "\n".join(parts), [])
        elif detail.startswith("the branch conflicts"):
            self.send_back(key, repo, f"{pr['url']} conflicts with {pr['baseRefName']}",
                           f"Rebase the branch on origin/{pr['baseRefName']}, resolve the conflicts keeping the ticket's intent, and re-run the checks.", [])
        else:
            self.send_back(key, repo, f"{len(info['feedback'])} feedback item(s) on {pr['url']}",
                           "Fix or answer every item; the branch must also be rebased on the latest base.", info["feedback"])

    def run_qa(self, key: str, view: dict[str, Any], repo: Repo, base: str, branch: str, pr: dict[str, Any]) -> bool:
        if self.attempts(key, "qa") >= self.cfg.max_attempts:
            self.block(key, "Too many failed QA attempts.", kind="Attempts")
            return True
        agent = self.pick_agent("qa", key, view.get("agent"))
        if agent is None:
            self.unserved(key, "qa", view.get("agent"))
            self.release_slot(key, repo.name)
            return False
        try:
            workdir, _ = repo.ensure_worktree(key, view.get("summary", key), base)
            repo.sync_worktree(workdir, branch)
        except GitError as error:
            self.block(key, f"Cannot get the pull request head on {self.cfg.machine}: {error}", kind="Config")
            return True
        self.label_workspace(key)
        self.jira.comment(key, f"AI Flow: QA {agent} on {self.cfg.machine} started on {pr['url']} at {pr['headRefOid'][:10]} "
                               f"(developer was {self.redis.get(f'dev:{key}') or '?'}).")
        why, text, found, quota = self.execute("qa", agent, key, view, workdir, branch, base, repo, slot_owner=key)
        if why == "lost-lease":
            return True
        if quota:
            self.quota_hit(key, agent, "qa")
            self.release_slot(key, repo.name)
            return True
        if not found or found[0] not in ("PASS", "FAIL"):
            self.failed(key, "qa", agent, why if why != "ok" else "no VERDICT line", text)
            self.release_slot(key, repo.name)
            return True
        self.redis.delete(f"attempts:{key}:qa")
        if found[0] == "FAIL":
            self.send_back(key, repo, f"QA {agent} FAILED the pull request: {found[1]}", text[-3000:], [])
            return True
        self.redis.set(f"qa:{key}", pr["headRefOid"], 30 * DAY)
        self.jira.comment(key, f"AI Flow: QA {agent} PASSED at {pr['headRefOid'][:10]}.\n\n{text[-800:]}")
        return True

    def merge(self, key: str, repo: Repo, gh: GitHub, pr: dict[str, Any], base: str, approved: bool = False) -> bool:
        """Merge the head that passed QA. `approved` (a human approved it): merge whatever head is current, QA is not repeated."""
        try:
            fresh = gh.view(pr["number"])
            if (not approved and fresh["headRefOid"] != self.state(key, "qa")) or repo.behind(base, pr["headRefName"]):
                self.redis.delete(f"qa:{key}")  # something moved since QA: start over
                return True
            gh.merge(pr["number"], fresh["headRefOid"], self.cfg.project(key).merge_method)
        except (GitHubError, GitError) as error:
            count = self.redis.incr(f"mergefail:{key}", DAY)
            self.jira.comment(key, f"AI Flow: merging {pr['url']} failed ({count}/3): {str(error)[:400]}")
            if count >= 3:
                self.block(key, f"Merging {pr['url']} keeps failing; needs the owner.", kind="Other")
            return count >= 3
        self.finish(key, repo, base, gh.view(pr["number"]), f"Merged {pr['url']} into {base} ({self.cfg.project(key).merge_method}) after CI, feedback and {'the human approval' if approved else 'QA'}.")
        return True

    # ---- Require human: Human review -> Ready to merge ------------------------------------------------------
    def to_human_review(self, key: str, repo: Repo, pr: dict[str, Any]) -> None:
        self.release_slot(key, repo.name)
        self.redis.set(f"humanreview:{key}", "1", 30 * DAY)
        self.jira.comment(key, f"AI Flow: CI, feedback and QA passed on {pr['url']} (head {pr['headRefOid'][:10]}). This ticket requires a human: "
                               f"review the pull request, then move the ticket to Ready to merge (the supervisor updates the branch if it is "
                               f"outdated and merges it) or to To Do with a comment to ask for changes. No preview deployment yet (JAIF-28).")
        self.jira.transition(key, "Human review")

    def declined(self, key: str, repo: Repo, pr: dict[str, Any]) -> None:
        repo.cleanup(key, remote=False)
        self.release_slot(key, repo.name)
        self.jira.comment(key, f"AI Flow: pull request {pr['url']} was closed without merging; declining the ticket.")
        self.jira.transition(key, "Declined")

    def pr_of(self, key: str, repo: Repo) -> tuple[GitHub, str, dict[str, Any]] | None:
        branch = repo.remote_branch(key)
        if not branch:
            self.block(key, "The ticket branch is not on origin, so there is no pull request to check.", kind="Config")
            return None
        gh = GitHub(repo.github_slug())
        pr = gh.find(branch)
        if pr is None:
            self.block(key, f"No pull request exists for branch {branch}.", kind="Config")
            return None
        return gh, branch, pr

    def human_wait(self, key: str, repo: Repo, base: str) -> bool:
        """Status Human review: leave the branch alone; only notice a merge, a close or new feedback."""
        self.redis.set(f"gatepoll:{key}", "1", self.cfg.gate_poll_seconds)
        try:
            found = self.pr_of(key, repo)
            if found is None:
                return True
            gh, _, pr = found
            if pr["state"] == "MERGED":
                self.finish(key, repo, base, pr, f"Pull request {pr['url']} was merged by a human into {base}.")
                return True
            if pr["state"] == "CLOSED":
                self.declined(key, repo, pr)
                return True
            feedback = gh.feedback(pr["number"], set(json.loads(self.state(key, "seen", "[]"))), self.cfg.ignored_authors)["items"]
        except (GitHubError, GitError, ValueError) as error:
            log(f"{key}: human review check failed: {error}")
            return False
        if feedback:
            self.send_back(key, repo, f"{len(feedback)} feedback item(s) on {pr['url']} during the human review",
                           "Fix or answer every item; the branch must also be rebased on the latest base.", feedback)
            return True
        self.note_gate(key, f"waiting for the human review of {pr['url']}")
        return False

    def finalize(self, key: str, repo: Repo, base: str) -> bool:
        """Status Ready to merge: the human approved. Bring the branch up to date and merge it once CI passes; QA is not repeated."""
        self.redis.set(f"gatepoll:{key}", "1", self.cfg.gate_poll_seconds)
        self.redis.set(f"humanok:{key}", "1", 30 * DAY)
        try:
            found = self.pr_of(key, repo)
            if found is None:
                return True
            gh, branch, pr = found
            info = self.gate_facts(key, repo, gh, base, branch, pr)
        except (GitHubError, GitError, ValueError) as error:
            log(f"{key}: ready-to-merge check failed: {error}")
            return False
        info.update(qa_ok=True, settled=True, require_human=False, auto_merge=True)
        action, detail = decide(info)
        if action == "done":
            self.finish(key, repo, base, pr, f"Pull request {pr['url']} is merged into {base}.")
        elif action == "closed":
            self.declined(key, repo, pr)
        elif action == "update":
            return self.update_branch(key, gh, pr, base, qa_again=False)
        elif action == "rework":
            self.rework_from_gate(key, repo, gh, pr, info, detail)
        elif action == "block":
            self.block(key, f"{detail} ({pr['url']}).", kind="CI" if "CI" in detail else "Human")
        elif action == "merge":
            if not self.take_slot(key, repo):
                self.note_gate(key, "waiting for another pull request of this repository to finish its merge")
                return False
            return self.merge(key, repo, gh, pr, base, approved=True)
        else:
            self.note_gate(key, f"{detail} ({pr['url']})")
            return False
        return True

    def finish(self, key: str, repo: Repo, base: str, pr: dict[str, Any], message: str) -> None:
        cleanup_status = "Worktree and branch removed."
        step = "refresh base"
        try:
            repo.refresh_base(base)
            step = "cleanup"
            repo.cleanup(key)
        except GitError as error:
            log(f"{key}: {step}: {error}")
            cleanup_status = f"Cleanup failed ({error}); remove the worktree by hand."
        self.release_slot(key, repo.name)
        self.jira.comment(key, f"AI Flow: {message} {cleanup_status}")
        self.jira.transition(key, "Done")
        self.jira.remove_label(key, "rework")
        for name in ("dev", "reviewer", "home", "cycles", "qa", "rework", "feedback", "seen", "pr", "prtitle", "pushed", "gateentry",
                     "gatenote", "mergefail", "gatepoll", "repo", "humanreview", "humanok", "blocked", "devmodel"):
            self.redis.delete(f"{name}:{key}")


def option(args: list[str], name: str) -> str | None:
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return args[index + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    from .config import ConfigError, load
    args = list(argv if argv is not None else sys.argv[1:])
    try:
        config = load(worker_id=option(args, "--id"))
        if "list" in args:  # worker numbers of this machine for the installer
            print("\n".join(str(n) for n in range(1, config.workers + 1)))
            return 0
        if "check" in args:  # the registry is loaded (and validated) above; show what was understood
            print(f"machine {config.machine}: {config.workers} worker(s), config {'from TOML files' if config.registry.from_files else 'legacy .env'}")
            for project in config.registry.projects.values():
                print(f"{project.jira_key}: repo {project.repo}, base {project.base}, hotfix {project.hotfix_base}, developer {project.developer or '-'}, "
                      f"reviewer {project.reviewer or '-'}, qa {project.qa or '-'}, human {project.require_human}, auto-merge {project.auto_merge}, "
                      f"ci {project.ci_required}, max parallel {project.max_parallel or 'unlimited'}")
            return 0
    except ConfigError as error:
        print(f"CONFIG ERROR: {error}", file=sys.stderr)
        return 2
    cfg = config
    redis = Redis(cfg.redis_host, cfg.redis_port, cfg.redis_db, cfg.redis_prefix, cfg.redis_password, cfg.redis_user)
    if "restart" in args:  # every worker leaves after its current step and systemd starts it again
        redis.set("restart", str(int(time.time())), 7 * 24 * 3600)  # workers started later ignore it
        print("restart requested for all workers")
        return 0
    supervisor = Supervisor(cfg, Jira(cfg.root), redis)
    (cfg.root / ".tmp").mkdir(exist_ok=True)
    if "--once" in args:
        return 0 if supervisor.tick() else 3
    supervisor.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
