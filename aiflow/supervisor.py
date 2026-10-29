"""The supervisor: pick up Jira tickets and drive them through developer, reviewer, the pull-request gate and QA."""
from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (agents, deploy, development_flow, events, github, master_rescue, operator_status, rework_rounds,
               runner, supervisor_execution, supervisor_flow, usage)
from .config import PROJECT_DIR, Settings
from .github import GitHub, GitHubError
from .gitops import GitError, Repo
from .incidents import find_incidents as find_incidents
from .jira import Jira, JiraError
from .lock import Redis, RedisError
from .master_rescue_worker import MasterRescueWorker
from .memory.workflow import MemoryWorkflow
from .project_database import ProjectDatabase, ticket_database_id
from .worktree_sweep import cleanup_done_worktree, resolve_merged_pull_request

STAGES = {"To Do": "developer", "In Progress": "developer", "In Review": "reviewer", "Review passed": "pr", "In QA": "gate",
          "Human review": "human", "Ready to merge": "finalize", master_rescue.HANDOVER: "master_rescue"}
PR_STAGES = ("In QA", "Human review", "Ready to merge")  # checked on GitHub by the supervisor, no AI to pick
ANY_AI = "Any"  # developer = "Any": the least busy AI takes the step
DAY = 86400
SLOT_SECONDS = 600


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}", flush=True)
    events.emit("log", message=message)  # every journal line is also an event in OpenObserve


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


SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
BACKMERGE = """## Business requirements
{base} holds {key} (a merged hotfix or release) and {develop} does not yet. {develop} must contain it, otherwise the next release loses it.

## AC
- [ ] `git merge origin/{base}` was run on this branch (a merge commit: no squash, no rebase) and every conflict is resolved keeping the intent of both sides.
- [ ] After the merge {develop} contains every commit of {base} and the repository tests pass.

## Scopes
Only the merge and the resolution of its conflicts. No new features, no refactoring.

## Tests
- The repository's own tests and CI.

## Technical
The branch starts from {develop}. Run `git merge origin/{base}`; keep the merge commit (do not rebase this branch). Commit message of a resolution: `chore: Merge {base} back into {develop} ({key})`.
"""


def parse_semver(text: str) -> tuple[int, int, int] | None:
    found = SEMVER.match((text or "").strip())
    return (int(found.group(1)), int(found.group(2)), int(found.group(3))) if found else None


def branches(project: Any, change: str, issue_type: str, description: str) -> tuple[str, str]:
    """(base, start): the branch the pull request targets and the branch the ticket branch starts from.
    A hotfix starts from and targets the production branch; a release starts from the integration branch and targets production."""
    if change == "Hotfix":
        return project.hotfix_base, project.hotfix_base
    if issue_type == "Release":
        return project.hotfix_base, project.base
    base = ticket_field(description, "Base", project.base)
    return base, base


def tail(text: str, size: int) -> str:
    """The last `size` characters of `text`, starting at a line boundary so a quote never begins in the middle of a line."""
    if len(text) <= size:
        return text
    cut = text[-size:]
    newline = cut.find("\n")
    return cut[newline + 1:] if 0 <= newline < size // 2 else cut


def ticket_of_branch(branch: str) -> str | None:
    """`JAIF-42-pr-gate-v2` -> `JAIF-42`; None for a branch that does not start with a Jira key."""
    match = re.match(r"^([A-Z][A-Z0-9]*-\d+)-", branch)
    return match.group(1) if match else None


def ticket_field(description: str, name: str, default: str) -> str:
    match = re.search(rf"\b{name}:\s*([\w./-]+)", description or "")
    return match.group(1) if match else default


def decide(info: dict[str, Any]) -> tuple[str, str]:
    """What the pull-request gate does next. Pure: `info` holds facts only.

    Returns (action, detail) with action one of done, closed, update, rework, block, wait, ready, qa, human, awaiting, merge."""
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
    if info.get("draft"):
        return "ready", ""  # CI is green: take the draft out of draft so the review bots comment
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
    def __init__(self, cfg: Settings, jira: Jira, redis: Redis, project_database: ProjectDatabase | None = None,
                 process_factory=None, deploy_log_dir: Path | None = None) -> None:
        self.cfg, self.jira, self.redis = cfg, jira, redis
        self.master_worker = MasterRescueWorker(self)
        self.project_database = project_database or ProjectDatabase(cfg.root)
        self.deploy_process_factory = process_factory or subprocess.Popen
        self.deploy_log_dir = deploy_log_dir or cfg.root / ".tmp" / "aiflow" / "logs"
        self.stop = False
        self.retired = False
        if hasattr(jira, "on_transition"):
            jira.on_transition = lambda key, to: self.record_status(key, to, "supervisor")
        self.memory_workflow = MemoryWorkflow(cfg, redis, jira, self.state, self.change_type, log)
        self._priorities: dict[str, tuple[float, str]] = {}

    # ---- selection -------------------------------------------------------------------------------------------
    def candidates(self) -> list[dict[str, Any]]:
        projects = ", ".join(self.cfg.projects)
        statuses = ", ".join(f'"{s}"' for s in (*STAGES, "Blocked"))
        jql = f"project in ({projects}) AND status in ({statuses}) AND issuetype != Epic ORDER BY priority DESC, created ASC"
        self.observe(found := self.jira.search(jql))
        found = [issue for issue in found if issue.get("status") in STAGES]
        found = [issue for issue in found if issue["key"] not in self.parents_of_open_work(found)]  # containers are never worked
        if self.cfg.home and self.cfg.slot == 1:  # a Claude worker can rescue a Handover ticket from another project
            found = [issue for issue in found if self.own_project(issue) or self.master_worker.active_record(issue)]
        return self.fair_order(found)

    def own_project(self, issue: dict[str, Any]) -> bool:
        return issue["key"].rsplit("-", 1)[0] == self.cfg.home

    def fair_order(self, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The worker's own project first (a shared worker has none); then rework; then the projects take turns: the first ticket of
        every project, then the second of every project, and so on. Inside a project the order of Jira (priority, then oldest) is
        kept; in one round the project served longest ago goes first."""
        rank: dict[str, int] = {}
        counts: dict[tuple[bool, str], int] = {}
        for issue in found:
            bucket = ("rework" in issue.get("labels", []), issue["key"].rsplit("-", 1)[0])
            rank[issue["key"]] = counts.get(bucket, 0)
            counts[bucket] = rank[issue["key"]] + 1
        projects = sorted({project for _, project in counts})
        served = dict(zip(projects, self.redis.get_many([f"served:{project}" for project in projects]))) if projects else {}

        def turn(issue: dict[str, Any]) -> tuple[bool, bool, int, float]:
            project = issue["key"].rsplit("-", 1)[0]
            value = served.get(project)
            return project != self.cfg.home, "rework" not in issue.get("labels", []), rank[issue["key"]], float(value) if value else 0.0
        return sorted(found, key=turn)  # stable

    def record_status(self, key: str, to: str, source: str) -> None:
        """A status change as an event, with the time spent in the status it left. Remembers the status for the next change."""
        now = time.time()
        previous = self.redis.get(f"status:{key}") or ""
        since = self.redis.get(f"statussince:{key}")
        if previous == to:
            return
        operator_status.record_status_change(self.redis, key, to, previous, source, since, now)
        self.redis.set_persistent(f"status:{key}", to)
        self.redis.set_persistent(f"statussince:{key}", str(now))

    def observe(self, issues: list[dict[str, Any]]) -> None:
        """Status changes nobody here made (the owner moved a ticket, Jira did): noticed at the next poll, at most one poll late.
        The Jira `updated` time is not the time of the change (a comment moves it too), so the poll time is the time of the event;
        a stale search result (older than our own last record) is ignored."""
        if not issues:
            return
        last = self.redis.get_many([f"status:{i['key']}" for i in issues])
        for issue, previous in zip(issues, last):
            if not issue.get("status") or previous == issue["status"]:
                continue
            since = self.redis.get(f"statussince:{issue['key']}")
            try:
                if since and parse_time(issue["updated"]).timestamp() + 2 < float(since):
                    continue  # the JQL index is behind a move this supervisor just made
            except (ValueError, KeyError):
                pass
            if self.redis.acquire(f"statuschg:{issue['key']}:{issue['status']}:{previous or 'none'}", self.cfg.owner, 3600):
                self.record_status(issue["key"], issue["status"], "observed")

    def parents_of_open_work(self, found: list[dict[str, Any]]) -> set[str]:
        """Keys among `found` that have child issues: a ticket with children is a container, only its leaves are worked."""
        keys = [issue["key"] for issue in found]
        if not keys:
            return set()
        try:
            children = self.jira.search(f"parent in ({', '.join(keys)})", 200)
        except JiraError as error:
            log(f"cannot look for child issues: {error}")
            return set(keys)  # when unsure, work nothing rather than a container
        return {child["parent"] for child in children if child.get("parent")}

    def eligible(self, issue: dict[str, Any]) -> bool:
        key, status = issue["key"], issue["status"]
        if status not in STAGES:  # the JQL index can lag behind a transition we just made
            return False
        labels = issue.get("labels", [])
        if ("aiflow-hold" in labels or "needs-owner" in labels
                or self.redis.get(f"lease:{key}")):
            return False
        if status == master_rescue.HANDOVER:
            return self.master_worker.active_record(issue) is not None
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
        if agent in self.cfg.agent_models:
            return self.cfg.agent_models[agent]  # [runtime] agent_models: the model this AI uses when the role names another AI or Any
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
            if preferred == ANY_AI:
                return self.least_loaded(present)
            return sorted(present, key=lambda a: a != preferred)  # empty field, or its AI is cooling: any other AI
        dev = self.redis.get(f"dev:{key}") or field_agent or ""
        reviewer = self.redis.get(f"reviewer:{key}")
        avoid = (reviewer,) if role == "qa" and reviewer else ()
        if preferred in present and preferred != dev and preferred not in avoid:
            return [preferred]
        other = agents.choose_other(dev, present, avoid=avoid)
        return [other] if other else []

    def least_loaded(self, present: list[str]) -> list[str]:
        """`developer = Any`: the AIs of `present` with a free session on this machine, the least busy (running / limit) first; ties follow
        agent_order. An AI with a limit of 2 or more keeps its last slot for review and QA; an AI without a limit counts as running / 8."""
        ranked = []
        for position, agent in enumerate(present):
            limit = self.cfg.ai_limits.get(agent.casefold())
            running = self.redis.session_count(self.cfg.machine, agent)
            if limit is not None and running >= (limit - 1 if limit >= 2 else limit):
                continue
            ranked.append((running / (limit or 8), position, agent))
        return [agent for _, _, agent in sorted(ranked)]

    def pick_agent(self, role: str, key: str, field_agent: str | None) -> str | None:
        found = self.eligible_agents(role, key, field_agent, self.usable_ais())
        return found[0] if found else None

    def reserve_agent(self, role: str, key: str, field_agent: str | None) -> tuple[str | None, str | None]:
        """Choose by the normal role rules and atomically reserve a local AI session slot."""
        pool = self.usable_ais()
        while pool:
            found = self.eligible_agents(role, key, field_agent, pool)
            if not found:
                return None, None
            for agent in found:
                owner = f"{self.cfg.owner}:{key}:{role}:{uuid.uuid4().hex}"
                limit = self.cfg.ai_limits.get(agent.casefold())
                if role == "developer" and not field_agent and self.role_spec(role, key)[0] == ANY_AI and limit is not None and limit >= 2:
                    limit -= 1  # `Any` keeps the last slot for review and QA, also when two developers choose at the same moment
                if self.redis.acquire_session(self.cfg.machine, agent, owner, self.cfg.lease_seconds, limit):
                    events.emit("ai.assign", ticket=key, role=role, agent=agent, limit=self.cfg.ai_limits.get(agent.casefold()),
                                from_field=bool(field_agent))
                    return agent, owner
                events.emit("ai.limit", ticket=key, role=role, agent=agent, limit=self.cfg.ai_limits.get(agent.casefold()))
                pool.remove(agent)
        return None, None

    def release_agent_session(self, key: str, role: str, agent: str, owner: str) -> None:
        try:
            self.redis.release_session(self.cfg.machine, agent, owner)
        except RedisError as error:
            log(f"{key}: could not release the {agent} {role} session slot ({error}); it expires by itself")

    def usable_ais(self) -> list[str]:
        """The installed AIs of this machine, without those the last probe found signed out or out of balance."""
        installed = agents.available(self.cfg.agent_order)
        states = self.redis.get_many([f"probe:{self.cfg.machine}:{a}" for a in installed])
        return [a for a, state in zip(installed, states) if not (state or "").startswith("down")]

    def probe_ais(self) -> None:
        """Ask each installed AI for one word every few hours, so one that lost its login leaves the pool before a ticket meets it."""
        if not self.redis.acquire("probing:" + self.cfg.machine, self.cfg.owner, 600):
            return
        probe_order = tuple(dict.fromkeys((*self.cfg.agent_order, master_rescue.RESCUE_AGENT)))
        for agent in agents.available(probe_order):
            name = f"probe:{self.cfg.machine}:{agent}"
            if self.redis.get(name) is not None:
                continue
            self.redis.set(f"hb:{self.cfg.machine}:{self.cfg.worker_label}", str(int(time.time())), 300)  # a probe can take a minute
            state = agents.probe(agent, self.cfg.root)
            events.emit("ai.probe", agent=agent, state=state)
            self.redis.set(name, state, 1800 if state.startswith("down") else self.cfg.probe_hours * 3600)
            if state != "ok":
                log(f"probe {agent} on {self.cfg.machine}: {state}")

    def publish_versions(self) -> None:
        """Once a day each machine publishes the versions of its tools, so the health check can see a drift between machines."""
        name = f"versions:{self.cfg.machine}"
        if self.redis.get(name) is None:
            self.redis.set(name, json.dumps(agents.tool_versions()), 2 * DAY)

    def paused(self) -> bool:
        return self.redis.get("paused") is not None

    def log_usage(self, key: str, role: str, agent: str, minutes: float, why: str, code: int | None, used: dict[str, Any] | None = None) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            self.redis.set(f"usage:{day}:{key}:{role}:{agent}:{int(time.time())}",
                           json.dumps({"key": key, "role": role, "ai": agent, "minutes": round(minutes, 1), "why": why, "code": code,
                                    "tokens": (used or {}).get("total", 0), "cost": (used or {}).get("cost_usd_list") or 0, "known": bool((used or {}).get("usage_known"))}), 10 * DAY)
        except RedisError as error:
            log(f"could not record the usage of {key}: {error}")

    def publish_ais(self) -> None:
        """Tell the other machines which AIs this one has (the entry expires when the machine stops beating)."""
        pool = self.usable_ais()
        if self.master_worker.online_here() and master_rescue.RESCUE_AGENT not in pool:
            pool.append(master_rescue.RESCUE_AGENT)
        self.redis.set(f"ais:{self.cfg.machine}", ",".join(pool), 600)

    def online_pool(self) -> list[str]:
        """AIs installed on this machine or on any machine that published recently."""
        pool = list(self.usable_ais())
        if self.master_worker.online_here() and master_rescue.RESCUE_AGENT not in pool:
            pool.append(master_rescue.RESCUE_AGENT)
        for name in self.redis.scan("ais:*"):
            for ai in (self.redis.get(name) or "").split(","):
                if ai and ai not in pool:
                    pool.append(ai)
        return sorted(pool, key=lambda a: self.cfg.agent_order.index(a) if a in self.cfg.agent_order else 99)

    def unserved(self, key: str, role: str, field_agent: str | None) -> None:
        """No AI on this machine can take the step. Another online machine may; if nobody can for long, block with the reason."""
        pool = self.online_pool()
        events.emit("ai.unserved", ticket=key, role=role, pool=",".join(pool), cooling=",".join(sorted(self.cooldowns(pool))))
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
        if self.paused():  # `aiflow-work pause`: no new work; resume with `aiflow-work resume`
            return False
        for issue in self.candidates():
            session_owner = None
            session_agent = None
            ticket_lease = False
            try:
                try:
                    if not self.eligible(issue):
                        continue
                    view = self.jira.view(issue["key"], full=True)
                    role = STAGES[issue["status"]]
                    view = supervisor_flow.stage_view(self, issue["key"], role, view)
                    if view is None:
                        continue
                    if not self.redis.acquire(f"lease:{issue['key']}", self.cfg.owner, self.cfg.lease_seconds):
                        continue
                    ticket_lease = True
                    agent = None
                    if role in ("developer", "reviewer", "master_rescue"):
                        agent, session_owner = supervisor_flow.reserve_stage_agent(self, role, issue["key"], view)
                        if agent is None:
                            continue
                        session_agent = agent
                except (JiraError, RuntimeError) as error:
                    log(f"skip {issue.get('key')}: {error}")
                    continue
                entered = self.redis.get(f"statussince:{issue['key']}")
                events.emit("ticket.pickup", ticket=issue["key"], status=issue["status"], role=role, agent=agent, rework="rework" in issue.get("labels", []),
                            lease_seconds=self.cfg.lease_seconds, waited_minutes=round((time.time() - float(entered)) / 60, 2) if entered else None)
                began_step = time.monotonic()
                worked = self.process(issue, view, role, agent, session_owner)
                if worked:
                    events.emit("step.run", ticket=issue["key"], status=issue["status"], role=role, agent=agent,
                                seconds=round(time.monotonic() - began_step, 1))
            finally:
                if session_owner and session_agent:
                    self.release_agent_session(issue["key"], role, session_agent, session_owner)
                if ticket_lease:
                    try:
                        self.redis.release(f"lease:{issue['key']}", self.cfg.owner)
                    except RedisError as error:
                        log(f"{issue['key']}: could not release the lease ({error}); it expires by itself")
            if worked:
                self.redis.set(f"served:{issue['key'].rsplit('-', 1)[0]}", str(time.time()), 7 * DAY)
                return True
        return False

    def run_forever(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))
        log(f"supervisor {self.cfg.owner} watching {','.join(self.cfg.projects)}")
        events.emit("worker.start", home=self.cfg.home, projects=",".join(self.cfg.projects), poll_seconds=self.cfg.poll_seconds)
        started = int(time.time())
        while not self.stop:
            if self.should_exit(started):
                break
            try:
                self.redis.set(f"hb:{self.cfg.machine}:{self.cfg.worker_label}", str(int(time.time())), 300)
                self.probe_ais()
                self.publish_versions()
                self.publish_ais()
                self.recheck_blocked()
                self.sweep_worktrees()
                self.sweep_containers()
                worked = self.tick()
            except Exception as error:  # keep the supervisor alive; the lease and Jira state stay consistent
                log(f"tick failed: {type(error).__name__}: {error}")
                events.emit("worker.error", error=f"{type(error).__name__}: {error}")
                worked = False
            if not worked:
                for _ in range(self.cfg.poll_seconds):
                    if self.stop:
                        break
                    time.sleep(1)

    def should_exit(self, started: int) -> bool:
        """Graceful exit between two tickets: after `aiflow-work restart` or after the maximum run time. systemd
        starts the worker again, so it runs the newest code and configuration."""
        if self.redis.get(f"retire:{self.cfg.machine}:{self.cfg.worker_label}") is not None:
            log("retired by the control (the project runs fewer workers now); leaving after the current ticket step")
            events.emit("worker.retired", home=self.cfg.home)
            self.retired = True
            return True
        asked = self.redis.get("restart")
        if asked is not None and int(asked) >= started:
            log("restart requested; leaving after the current ticket step")
            events.emit("worker.restart")
            return True
        limit = self.cfg.worker_max_minutes
        if limit > 0 and time.time() - started > limit * 60:
            log(f"worker ran for {limit} minutes; leaving so systemd starts a fresh one")
            return True
        return False

    # ---- one ticket ------------------------------------------------------------------------------------------
    def process(self, issue: dict[str, Any], view: dict[str, Any], role: str, agent: str | None,
                session_owner: str | None = None) -> bool:
        """Returns True when something was done (so the loop does not sleep), False when the ticket only waits."""
        return supervisor_flow.process_ticket(self, issue, view, role, agent, session_owner, branches, ticket_field, log)

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

    def release_ready(self, key: str, repo: Repo) -> bool:
        """A Release ticket needs a version (field Version, x.y.z) higher than the last tag; otherwise it is blocked as unclear."""
        project = self.cfg.project(key)
        try:
            version = str(self.jira.read_field(key, self.cfg.version_field) or "").strip()
        except JiraError as error:
            log(f"{key}: cannot read Version: {error}")
            return False
        wanted, last = parse_semver(version), repo.last_tag(project.tag_prefix)
        if wanted is None:
            self.block(key, f"The field Version must be a semantic version such as 1.2.0, not `{version}`.", kind="Unclear")
            return False
        if last and parse_semver(last) is not None and wanted <= parse_semver(last):
            self.block(key, f"Version {version} is not higher than the last release {last}.", kind="Unclear")
            return False
        self.redis.set(f"version:{key}", ".".join(map(str, wanted)), 60 * DAY)
        self.redis.set(f"lasttag:{key}", last, 60 * DAY)
        return True

    def backmerge(self, key: str, repo: Repo, project: Any, base: str) -> None:
        """After a merge into the production branch (hotfix, release) the integration branch must get it back: open a ticket whose
        developer merges and resolves conflicts, and which then goes through the normal flow."""
        develop = project.base
        if base != project.hotfix_base or base == develop or self.state(key, "backmerge") or repo.contains(develop, base):
            return
        new = self.jira.create_issue(project.jira_key, f"Merge {base} back into {develop} after {key}",
                                     BACKMERGE.format(base=base, develop=develop, key=key), "Task")
        self.jira.set_field(new, self.cfg.type_field, {"value": "Chore"})
        self.jira.link(key, new)
        self.redis.set(f"backmerge:{key}", new, 60 * DAY)
        self.jira.transition(new, "To Do")
        self.jira.comment(key, f"AI Flow: {base} holds changes that {develop} does not; {new} merges {base} back into {develop}.")

    def after_merge(self, key: str, repo: Repo, base: str) -> None:
        """What follows a merge: the tag and GitHub release of a Release ticket, and the merge back of the production branch."""
        project = self.cfg.project(key)
        try:
            self.deploy_after_merge(key, repo, base, project)
        except Exception as error:  # A post-merge hook must never undo or block an already completed merge.
            log(f"{key}: deploy hook failed after merge ({type(error).__name__})")
            self.record_deploy_failure(key, project, f"runner error ({type(error).__name__})",
                                       "not created; post-merge runner failed before writing output")
            try:
                self.jira.comment(key, f"AI Flow post-merge command could not complete ({type(error).__name__}); the merge remains complete.")
            except JiraError:
                pass
        try:
            if self.state(key, "isrelease"):
                tag = project.tag_prefix + self.state(key, "version")
                url = GitHub(repo.github_slug()).release(tag, base, f"{project.name or repo.name} {tag}")
                self.jira.comment(key, f"AI Flow: released {tag}: {url}")
            self.backmerge(key, repo, project, base)
        except (GitHubError, GitError, JiraError) as error:
            log(f"{key}: follow-up after the merge failed: {error}")
            self.jira.comment(key, f"AI Flow: the pull request is merged, but the follow-up (release tag or merge back) failed: {error}. "
                                   f"Finish it by hand or tell the supervisor.")

    def record_deploy_failure(self, key: str, project: Any, outcome: str, log_reference: str) -> None:
        state = json.dumps({"ticket": key, "outcome": outcome, "log": log_reference,
                            "at": datetime.now(timezone.utc).isoformat()})
        try:
            self.redis.set(f"deploy-failure:{project.jira_key}", state, 30 * DAY)
        except RedisError:
            log(f"{key}: could not record deploy failure because Redis was unavailable")

    def deploy_after_merge(self, key: str, repo: Repo, base: str, project: Any) -> None:
        """Serialize main-clone refreshes and coalesce eligible post-merge commands per project."""
        if not project.deploy_command.strip():
            return
        special_merge = self.state(key, "ishotfix") or self.state(key, "isrelease")
        eligible = project.deploy_on == "all" or not (special_merge or base != project.base)
        lock_name = f"deploy-lock:{project.jira_key}"
        pending_name = f"deploy-pending:{project.jira_key}"
        active_name = f"deploy-active:{project.jira_key}"
        seen_name = f"deploy-seen:{project.jira_key}:{key}"
        owner = f"{self.cfg.owner}:{uuid.uuid4().hex}"
        ttl = project.deploy_timeout_minutes * 60 + 300
        event = json.dumps({"ticket": key, "base": base, "eligible": eligible}, separators=(",", ":"))
        try:
            queued = self.redis.enqueue_deploy(lock_name, pending_name, active_name, seen_name, owner, key, event, ttl, 30 * DAY)
        except RedisError:
            log(f"{key}: deploy hook skipped because Redis was unavailable for the project lock")
            if eligible:
                try:
                    self.jira.comment(key, "AI Flow post-merge command was skipped because its project lock was unavailable; the merge remains complete.")
                except JiraError:
                    pass
            return
        events.emit("deploy.queue", ticket=key, project=project.jira_key, base=base, eligible=eligible, queued=queued)
        if queued == 2:
            log(f"{key}: duplicate post-merge event was already queued for project {project.jira_key}")
            return
        if queued == 0:
            log(f"{key}: queued post-merge work for project {project.jira_key}")
            return

        batch = [event]
        comments: list[tuple[str, str]] = []
        while batch:
            parsed_events = [json.loads(item) for item in batch]
            eligible_events = [item for item in parsed_events if item["eligible"]]
            deploy_base = eligible_events[-1]["base"] if eligible_events else ""
            for refresh_base in dict.fromkeys(item["base"] for item in parsed_events if item["base"] != deploy_base):
                try:
                    repo.refresh_base(refresh_base)
                except GitError as error:
                    log(f"{key}: queued merge base refresh failed ({type(error).__name__})")

            refresh_failed = False
            if deploy_base:
                try:
                    repo.checkout_deploy_base(deploy_base)
                except GitError as error:
                    refresh_failed = True
                    log(f"{key}: deploy skipped because the main clone could not be prepared ({type(error).__name__})")

            if eligible_events and not refresh_failed:
                def heartbeat() -> bool:
                    try:
                        self.redis.set(f"hb:{self.cfg.machine}:{self.cfg.worker_label}", str(int(time.time())), 300)
                        return self.redis.renew(lock_name, owner, ttl)
                    except RedisError:
                        log(f"{key}: deploy stopped because Redis heartbeat renewal failed")
                        return False

                try:
                    result = deploy.run(project.deploy_command, repo.path, project.deploy_timeout_minutes,
                                        self.deploy_log_dir, self.deploy_process_factory,
                                        heartbeat_seconds=self.cfg.heartbeat_seconds, heartbeat=heartbeat)
                except Exception as error:
                    outcome = f"runner error ({type(error).__name__})"
                    self.record_deploy_failure(eligible_events[-1]["ticket"], project, outcome,
                                               "not created; post-merge runner failed before writing output")
                    summary = f"AI Flow post-merge command could not complete ({type(error).__name__}); the merge remains complete."
                    comments.extend((item["ticket"], summary) for item in eligible_events)
                else:
                    try:
                        log_reference = str(result.log_path.relative_to(self.cfg.root))
                    except ValueError:
                        log_reference = str(result.log_path)
                    events.emit("deploy.result", ticket=eligible_events[-1]["ticket"], project=project.jira_key, ok=result.succeeded,
                                timed_out=result.timed_out, exit_code=result.exit_code, log=log_reference)
                    if result.succeeded:
                        try:
                            self.redis.delete(f"deploy-failure:{project.jira_key}")
                        except RedisError:
                            log(f"{key}: could not clear deployment health state because Redis was unavailable")
                    else:
                        outcome = (f"timeout after {project.deploy_timeout_minutes} minute(s)" if result.timed_out
                                   else f"exit code {result.exit_code}")
                        self.record_deploy_failure(eligible_events[-1]["ticket"], project, outcome, log_reference)
                    summary = deploy.comment(result, project.deploy_timeout_minutes, log_reference)
                    comments.extend((item["ticket"], summary) for item in eligible_events)
            elif eligible_events and refresh_failed:
                state = json.dumps({"ticket": eligible_events[-1]["ticket"], "outcome": "main clone refresh failed",
                                    "log": "not created; main clone could not be prepared",
                                    "at": datetime.now(timezone.utc).isoformat()})
                try:
                    self.redis.set(f"deploy-failure:{project.jira_key}", state, 30 * DAY)
                except RedisError:
                    log(f"{key}: could not record deploy health state because Redis was unavailable")
                comments.extend((item["ticket"], "AI Flow post-merge command was skipped because the main clone could not be prepared; the merge remains complete.")
                                for item in eligible_events)

            try:
                pending = self.redis.finish_deploy(lock_name, pending_name, active_name, owner, ttl, "\n".join(batch))
            except RedisError:
                log(f"{key}: could not finish the project deployment lock; it expires automatically")
                pending = ""
            if pending == -1:
                log(f"{key}: project deployment lock ownership was lost")
                break
            batch = pending.splitlines() if isinstance(pending, str) and pending else []
        for ticket, summary in comments:
            try:
                self.jira.comment(ticket, summary)
            except JiraError:
                log(f"{ticket}: could not post the post-merge command result")

    def attempts(self, key: str, role: str) -> int:
        return int(self.redis.get(f"attempts:{key}:{role}") or 0)

    def block(self, key: str, reason: str, kind: str = "Other") -> None:
        """Move the ticket to Blocked, say why on the ticket and in the field Blocked reason, and remember where it came from."""
        self.release_slot(key)
        rework_rounds = int(self.redis.get(f"cycles:{key}") or 0) if kind == "Rework" else 0
        attempts = {role: count for role in ("developer", "reviewer", "qa", "pr")
                    if (count := self.attempts(key, role)) > 0}
        status = self.blocked_from_status(key)
        if self.save_block_snapshot(key, status, kind, reason, rework_rounds, attempts):
            self.clear_block_counters(key)
        try:
            self.jira.set_field(key, self.cfg.blocked_reason_field, f"{kind}: {reason}"[:250])
        except JiraError as error:
            log(f"{key}: could not record the block reason: {error}")
        events.emit("ticket.blocked", ticket=key, kind=kind, reason=reason[:500])
        self.jira.comment(key, f"AI Flow: blocked on {self.cfg.machine} ({kind}). {reason}")
        self.jira.transition(key, "Blocked")

    def blocked_from_status(self, key: str) -> str:
        """Read the previous Jira status, falling back to the supervisor's last observed status."""
        try:
            issue = self.jira.view(key)
            status = issue.get("status", "") if isinstance(issue, dict) else ""
            if isinstance(status, str) and status:
                return status
        except (JiraError, ValueError, TypeError, OSError, subprocess.TimeoutExpired) as error:
            log(f"{key}: could not read Jira status while preserving the block snapshot ({type(error).__name__})")
        try:
            return str(self.redis.get(f"status:{key}") or "unknown")
        except RedisError as error:
            log(f"{key}: could not read the cached status for the block snapshot ({type(error).__name__})")
            return "unknown"

    def save_block_snapshot(self, key: str, status: str, kind: str, reason: str,
                            rework_rounds: int, attempts: dict[str, int]) -> bool:
        blocked = {"status": status, "kind": kind, "reason": reason[:500]}
        if rework_rounds:
            blocked["rework_rounds"] = rework_rounds
        if attempts:
            blocked["attempts"] = attempts
        try:
            self.redis.set(f"blocked:{key}", json.dumps(blocked), 30 * DAY)
        except RedisError as error:
            log(f"{key}: could not preserve the block snapshot ({type(error).__name__})")
            return False
        return True

    def clear_block_counters(self, key: str) -> None:
        """Clear each saved attempt counter independently after the block snapshot is durable."""
        for name in ("attempts:%s:developer", "attempts:%s:reviewer", "attempts:%s:qa", "cycles:%s"):
            try:
                self.redis.delete(name % key)
            except RedisError as error:
                log(f"{key}: could not clear block counter {name.split(':')[-1]} ({type(error).__name__})")

    def prepare_ticket_database(self, key: str, worktree: Path) -> bool:
        """Provision the configured project database before an AI uses its ticket worktree."""
        project = self.cfg.project(key)
        if not project.database:
            return True
        if not self.project_database.env_is_ignored(worktree):
            log(f"{key}: ticket database setup failed (worktree .env is not ignored)")
            self.block(key, "Worktree .env is not ignored by Git; refusing to write shared credentials.", kind="Config")
            return False
        try:
            ticket_database_id(key)
        except ValueError:
            self.block(key, "Ticket key cannot be mapped to a 1-12 character database id.", kind="Config")
            return False
        self.redis.set_persistent(f"dbticket:{key}", project.database)
        self.redis.set_persistent(f"dbworktree:{key}:{self.cfg.machine}", "1")
        failure = self.project_database.prepare(project.database, key, worktree)
        if failure is None:
            return True
        log(f"{key}: ticket database setup failed ({failure})")
        self.block(key, f"Ticket database setup failed: {failure}.", kind="Config")
        return False

    def teardown_ticket_database(self, key: str) -> None:
        """Remove a configured ticket database and leave failures for the health check to report."""
        database_name = self.redis.get(f"dbticket:{key}")
        if not database_name:
            return
        if self.project_database.teardown(database_name, key):
            self.redis.delete(f"dbticket:{key}")
            self.redis.delete(f"dbcleanup:{key}")
            for marker in self.redis.scan(f"dbworktree:{key}:*"):
                self.redis.delete(marker)
            return
        self.redis.set_persistent(f"dbcleanup:{key}", "1")
        log(f"{key}: ticket database teardown failed; health check will report it")

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
                events.emit("ticket.resumed", ticket=key, to=info["status"], role=role)
                self.jira.set_field(key, self.cfg.blocked_reason_field, None)
                self.jira.comment(key, f"AI Flow: an AI for the {role} step is available again; the ticket returns to {info['status']}.")
                for name in (f"blocked:{key}", f"waiting:{key}:{role}", f"waitnote:{key}:{role}"):
                    self.redis.delete(name)

    def clear_block(self, key: str) -> None:
        """The owner moved a blocked ticket back to To Do himself: forget why it was blocked (the answer is in the comments)."""
        if self.redis.get(f"blocked:{key}") is None:
            return
        rework_rounds.forget_block(self.redis, key)
        events.emit("ticket.unblocked", ticket=key)
        self.clear_blocked_reason(key)

    def clear_blocked_reason(self, key: str) -> None:
        """Clear a stale Jira Blocked reason after a ticket becomes terminal or is unblocked."""
        try:
            self.jira.set_field(key, self.cfg.blocked_reason_field, None)
        except JiraError as error:
            log(f"{key}: cannot clear Blocked reason: {error}")

    def sweep_containers(self) -> None:
        """A ticket whose children are all Done is Done too: close it with a summary of the children."""
        if not self.redis.acquire("containers", self.cfg.owner, 600):
            return
        projects = ", ".join(self.cfg.projects)
        try:
            children = self.jira.search(f"project in ({projects}) AND parent is not EMPTY", 500)
        except JiraError as error:
            log(f"container sweep: {error}")
            return
        by_parent: dict[str, list[dict[str, Any]]] = {}
        for child in children:
            by_parent.setdefault(child["parent"], []).append(child)
        for parent, kids in by_parent.items():
            alive = [k for k in kids if k["status"] != "Declined"]
            if not alive or any(k["status"] != "Done" for k in alive):
                continue
            try:
                state = self.jira.search(f"key = {parent}", 1)
                if not state or state[0]["status"] in ("Done", "Declined") or self.redis.get(f"lease:{parent}"):
                    continue
                if self.close_container(parent, state[0]["status"]):
                    lines = "\n".join(f"- {k['key']} {k.get('summary', '')}" for k in alive)
                    self.jira.comment(parent, f"AI Flow: all {len(alive)} child ticket(s) are Done, so this ticket is Done.\n\n{lines}")
            except JiraError as error:
                log(f"{parent}: cannot close the container: {error}")

    def close_container(self, key: str, status: str) -> bool:
        """Walk a container through the workflow to Done. A project whose workflow lacks a step is left alone."""
        path = ["Backlog", "To Do", "In Progress", "In Review", "Review passed", "In QA", "Done"]
        if status == "Blocked":  # a container that was blocked (for example worked before it had children) goes back to To Do first
            if not self.jira.transition(key, "To Do"):
                return False
            try:
                self.jira.set_field(key, self.cfg.blocked_reason_field, None)
            except JiraError as error:
                log(f"{key}: cannot clear Blocked reason: {error}")
            status = "To Do"
        if status not in path:
            return False
        for step in path[path.index(status) + 1:]:
            if step == "Review passed" and not self.jira.transition(key, step):
                continue  # a workflow without that status goes on to In QA
            elif step != "Review passed" and not self.jira.transition(key, step):
                log(f"{key}: cannot move to {step}; the container stays where it is")
                return False
        return True

    def sweep_worktrees(self) -> None:
        """Remove the worktrees of tickets that are Done (and their branches) or Declined (the branch stays) on this machine."""
        if not self.redis.acquire("sweep:" + self.cfg.machine, self.cfg.owner, 1800):
            return
        for project in self.cfg.registry.projects.values():
            repo = Repo(self.cfg.root, project.repo, project.company)
            if not repo.exists():
                continue
            try:
                trees = {ticket_of_branch(branch): (path, branch) for path, branch in repo.worktrees() if ticket_of_branch(branch)}
                if not trees:
                    continue
                for issue in self.jira.search(f"key in ({', '.join(trees)})", 100):
                    path, branch = trees[issue["key"]]
                    if issue["status"] == "Done":
                        merged_pr, lookup_error = resolve_merged_pull_request(repo, issue["key"], branch, self.gate_pr)
                        cleanup_done_worktree(self.redis, repo, self.cfg.machine, issue["key"], branch, merged_pr, lookup_error, log)
                    elif issue["status"] == "Declined":
                        repo.remove_worktree(path)
                        self.redis.delete(f"sweep-cleanup:{self.cfg.machine}:{issue['key']}:{branch}")
                        log(f"{issue['key']}: removed the worktree of a declined ticket (branch {branch} kept)")
            except (GitError, JiraError, OSError) as error:
                log(f"sweep {project.repo}: {error}")
        self.sweep_ticket_databases()

    def sweep_ticket_databases(self) -> None:
        """Tear down terminal tickets and tickets whose last registered worktree was removed."""
        for marker in self.redis.scan("dbticket:*"):
            key = marker.removeprefix("dbticket:")
            try:
                status = str(self.jira.view(key).get("status", ""))
            except JiraError as error:
                log(f"{key}: cannot check ticket database cleanup status: {error}")
                continue
            if status in ("Done", "Declined"):
                self.teardown_ticket_database(key)
                continue
            local_marker = f"dbworktree:{key}:{self.cfg.machine}"
            if self.redis.get(local_marker) is None:
                continue
            project = self.cfg.project(key)
            repo = Repo(self.cfg.root, project.repo, project.company)
            if not repo.exists():
                continue
            try:
                if repo.find_worktree(key) is not None:
                    continue
            except GitError as error:
                log(f"{key}: cannot check ticket worktree for database cleanup: {error}")
                continue
            self.redis.delete(local_marker)
            if not self.redis.scan(f"dbworktree:{key}:*"):
                self.teardown_ticket_database(key)

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
                repo: Repo, slot_owner: str | None = None, session_owner: str | None = None,
                model_override: tuple[str, str] | None = None,
                rescue_deadline: float | None = None) -> tuple[str, str, tuple[str, str] | None, bool]:
        """Run one AI. Returns (why, final text, verdict, quota)."""
        return supervisor_execution.execute(self, role, agent, key, view, workdir, branch, base, repo,
                                            slot_owner, session_owner, model_override, rescue_deadline,
                                            SLOT_SECONDS, log)

    def execution_model(self, role: str, agent: str, key: str, override: tuple[str, str] | None) -> tuple[str, str]:
        return override if override is not None else self.model_for(role, agent, key)

    def renew_ticket_ownership(self, key: str, agent: str, session_owner: str | None = None) -> bool | None:
        """Renew ticket and worker leases; None means Redis could not confirm ownership."""
        try:
            if not self.redis.renew(f"lease:{key}", self.cfg.owner, self.cfg.lease_seconds):
                return False
            return (not session_owner or self.redis.renew_session(
                self.cfg.machine, agent, session_owner, self.cfg.lease_seconds))
        except RedisError:
            return None

    def quota_hit(self, key: str, agent: str, role: str) -> None:
        self.redis.set(f"cooldown:{agent}", "1", self.cfg.quota_cooldown_minutes * 60)
        events.emit("ai.quota", ticket=key, role=role, agent=agent, cooldown_minutes=self.cfg.quota_cooldown_minutes)
        self.jira.comment(key, f"AI Flow: {agent} is not usable on {self.cfg.machine} during {role} (usage limit, empty balance or sign-in problem). "
                               f"Cooling it down for {self.cfg.quota_cooldown_minutes} min; the ticket will be reassigned to another AI "
                               f"and continues from the branch and the hand-over comments.")

    def failed(self, key: str, role: str, agent: str, why: str, text: str, output_allowed: bool = True) -> None:
        count = self.redis.incr(f"attempts:{key}:{role}", 7 * DAY)
        safe_why = why if output_allowed else "details omitted for rescue privacy"
        events.emit("ai.failed", ticket=key, role=role, agent=agent, why=safe_why,
                    attempt=count, of=self.cfg.max_attempts)
        output = f" Last output: {text[-600:]!r}" if text and output_allowed else ""
        self.jira.comment(key, f"AI Flow: {agent} on {self.cfg.machine} ended the {role} run without a usable result ({safe_why}); "
                               f"attempt {count}/{self.cfg.max_attempts}.{output}")
        if count >= self.cfg.max_attempts:
            self.block(key, f"{self.cfg.max_attempts} failed {role} attempts; needs the owner.", kind="Attempts")

    # ---- developer -------------------------------------------------------------------------------------------
    def develop(self, key: str, issue: dict[str, Any], view: dict[str, Any], repo: Repo, base: str, agent: str,
                start: str = "", session_owner: str | None = None,
                rescue_record: dict[str, Any] | None = None) -> bool:
        return development_flow.develop(self, key, issue, view, repo, base, agent, start, session_owner, rescue_record, log)

    def finish_master_attempt(self, key: str, view: dict[str, Any], repo: Repo, base: str, agent: str,
                              workdir: Path, branch: str, text: str, why: str,
                              found: tuple[str, str] | None, quota: bool,
                              session_owner: str | None = None) -> bool:
        return self.master_worker.finish_attempt(key, view, repo, base, agent, workdir, branch, text, why, found, quota,
                                                 session_owner)

    def end_master_rescue(self, key: str, ok: bool, cause: str = "", findings: str = "") -> bool:
        return self.master_worker.end(key, ok, cause, findings)

    def begin_master_completion(self, key: str) -> bool:
        return self.master_worker.begin_completion(key)

    def finish_development(self, key: str, view: dict[str, Any], repo: Repo, base: str, agent: str, workdir: Path,
                           branch: str, text: str, rescue_run: bool = False,
                           session_owner: str | None = None) -> bool | None:
        if self.renew_ticket_ownership(key, agent, session_owner) is not True:
            return None
        answers = self.prepare_development_handoff(key, view, repo, base, agent, workdir, text, rescue_run)
        if answers is None:
            return False
        if self.renew_ticket_ownership(key, agent, session_owner) is not True:
            return None
        try:
            repo.push(workdir, branch, lease=True)
        except GitError as error:
            self.failed(key, "developer", agent, f"push failed: {error}", text, not rescue_run)
            return False
        if self.renew_ticket_ownership(key, agent, session_owner) is not True:
            return None
        self.redis.set(f"pushed:{key}", str(int(time.time())), 30 * DAY)
        self.require_human_for_deployment(key)
        for name in ("attempts:%s:developer", "qa:%s"):
            self.redis.delete(name % key)
        if answers:
            if self.renew_ticket_ownership(key, agent, session_owner) is not True:
                return None
            if self.post_replies(key, repo, answers):
                return False
        if self.renew_ticket_ownership(key, agent, session_owner) is not True:
            return None
        return self.comment_development_handoff(key, agent, branch, text, rescue_run, session_owner)

    def prepare_development_handoff(self, key: str, view: dict[str, Any], repo: Repo, base: str, agent: str,
                                   workdir: Path, text: str, rescue_run: bool) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
        if self.cfg.project(key).database:
            try:
                if repo.ticket_env_committed(workdir, base):
                    self.block(key, "The ticket worktree .env was committed; remove it from the branch before continuing.", kind="Config")
                    return None
            except GitError as error:
                self.failed(key, "developer", agent, "worktree .env safety check", str(error), not rescue_run)
                return None
        bad = [s for s in repo.commit_subjects(workdir, base) if not github.valid_title(s)]
        if bad:
            self.failed(key, "developer", agent, "commit messages break Conventional Commits "
                        "(type(scope): Subject, uppercase first letter, no final period, 10-120 chars): " + "; ".join(bad[:4]),
                        text, not rescue_run)
            return None
        answers = self.read_replies(key)
        if isinstance(answers, str):
            self.failed(key, "developer", agent, answers, text, not rescue_run)
            return None
        title = "" if rescue_run else agents.pr_title(text) or ""
        if not github.valid_title(title):
            title = github.fallback_title(key, view.get("summary", key), str(view.get("type", "")), self.change_type(key))
        self.redis.set(f"prtitle:{key}", github.with_key(title, key), 30 * DAY)
        return answers

    def comment_development_handoff(self, key: str, agent: str, branch: str, text: str,
                                   rescue_run: bool, session_owner: str | None = None) -> bool | None:
        output = "" if rescue_run else f"\n\n{tail(text, 1000 if self.state(key, 'humanok') else 1500)}"
        if self.state(key, "humanok"):  # a human already approved: no review, no QA again, only CI
            self.jira.comment(key, f"AI Flow: {agent} finished the rework on {self.cfg.machine}. Branch {branch} pushed; the human approved "
                                   f"this ticket before, so it goes back to Ready to merge (CI must pass).{output}")
            if self.renew_ticket_ownership(key, agent, session_owner) is not True:
                return None
            return self.jira.transition(key, "Ready to merge")
        self.jira.comment(key, f"AI Flow: {agent} finished on {self.cfg.machine}. Branch {branch} pushed to origin; sending to review.{output}")
        if self.renew_ticket_ownership(key, agent, session_owner) is not True:
            return None
        return self.jira.transition(key, "In Review")

    def require_human_for_deployment(self, key: str) -> None:
        """A Deployment hand-over needs a human unless the project opts out."""
        try:
            latest = self.jira.comments(key, 3)
            found = any(deployment_needed(str(c.get("body", ""))) for c in latest if "## Deployment" in str(c.get("body", "")))
            if not found:
                return
            if not self.cfg.project(key).human_on_deployment:
                if not self.state(key, "deployment-skipped"):
                    message = "Deployment section found; human review skipped by project setting"
                    if not any(message in str(c.get("body", "")) for c in latest):
                        self.jira.comment(key, message)
                    self.redis.set(f"deployment-skipped:{key}", "1", 30 * DAY)
                return
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
    def review(self, key: str, view: dict[str, Any], repo: Repo, base: str, agent: str, session_owner: str | None = None) -> bool:
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
        if not self.prepare_ticket_database(key, workdir):
            return True
        if repo.ahead(workdir, base) == 0:
            self.block(key, "The ticket branch has no commits ahead of the base; developer output is missing.", kind="Other")
            return True
        self.redis.set(f"reviewer:{key}", agent, 30 * DAY)
        self.label_workspace(key)
        self.jira.comment(key, f"AI Flow: reviewer {agent} on {self.cfg.machine} started (developer was {self.redis.get(f'dev:{key}') or '?'}).")
        why, text, found, quota = self.execute("reviewer", agent, key, view, workdir, branch, base, repo,
                                               session_owner=session_owner)
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
        self.redis.set(f"reviewtext:{key}", text[-6000:], 30 * DAY)
        self.jira.comment(key, f"AI Flow: reviewer {agent} PASSED.\n\n{tail(text, 1500)}")
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
                found = gh.create(branch, base, title, self.pr_body(key, view, repo, base, agent), draft=True)
                self.redis.delete(f"readyat:{key}")
                review = self.state(key, "reviewtext")
                if review:
                    gh.comment(found["number"], f"### Review by {agent}\n\n{review}")
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
        events.emit("ticket.rework", ticket=key, round=cycles, headline=headline[:300], items=len(items))
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
        over_limit = rework_rounds.record_round(self.redis, key, cycles, text, (headline, details, items), self.cfg.loop_limit_for(key), 30 * DAY)
        self.redis.delete(f"qa:{key}")
        self.jira.comment(key, f"AI Flow: {text[:6000]}")
        if over_limit:
            self.block(key, over_limit, kind="Rework")
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

    def gate_pr(self, key: str, gh: GitHub) -> dict[str, Any] | None:
        """Read the known PR before checking origin, where a merged PR's branch may already be gone."""
        number = self.state(key, "pr")
        if number:
            return gh.view(int(number))
        comments = getattr(self.jira, "comments", None)
        if not callable(comments):
            return None
        for comment in reversed(comments(key, 12)):
            match = re.search(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)", str(comment.get("body", "")))
            if match:
                return gh.view(int(match.group(1)))
        return None

    def gate_is_terminal(self, key: str, repo: Repo, base: str, gh: GitHub, pr: dict[str, Any]) -> bool | None:
        """Refresh Jira and GitHub; return true when Done or merged, false when still active, None on read failure."""
        try:
            current = self.jira.view(key)
            if current.get("status") == "Done":
                self.clear_blocked_reason(key)
                self.memory_workflow.remember_case(key, repo, pr)
                return True
            pr = gh.view(int(pr["number"]))
            if pr.get("state") == "MERGED":
                self.finish(key, repo, base, pr, f"Pull request {pr['url']} is merged into {base}.", merged_pr=True)
                return True
        except (JiraError, GitHubError, ValueError, KeyError) as error:
            log(f"{key}: cannot confirm ticket and pull-request state ({error})")
            return None
        return False

    def block_gate(self, key: str, repo: Repo, base: str, gh: GitHub, pr: dict[str, Any] | None, reason: str,
                   kind: str = "Config") -> bool:
        """Re-read Jira and GitHub immediately before a gate block so stale workers cannot block merged work."""
        if pr is not None:
            terminal = self.gate_is_terminal(key, repo, base, gh, pr)
            if terminal is not False:
                return terminal is True
        else:
            try:
                if self.jira.view(key).get("status") == "Done":
                    self.clear_blocked_reason(key)
                    self.memory_workflow.remember_case(key, repo, {})
                    return True
            except JiraError as error:
                log(f"{key}: cannot confirm ticket state before blocking ({error})")
                return False
        self.block(key, reason, kind=kind)
        return True

    def _gate(self, key: str, view: dict[str, Any], repo: Repo, base: str) -> bool:
        try:
            current = self.jira.view(key)
            if current.get("status") == "Done":
                self.clear_blocked_reason(key)
                self.memory_workflow.remember_case(key, repo, {})
                return True
            gh = GitHub(repo.github_slug())
            pr = self.gate_pr(key, gh)
            if pr is not None and pr.get("state") == "MERGED":
                self.finish(key, repo, base, pr, f"Pull request {pr['url']} is merged into {base}.", merged_pr=True)
                return True
            branch = repo.remote_branch(key)
            if branch and (pr is None or pr.get("headRefName") != branch):
                pr = gh.find(branch)
            if pr is None:
                reason = ("The ticket branch is not on origin, so there is no pull request to check." if not branch
                          else f"No pull request exists for branch {branch}.")
                return self.block_gate(key, repo, base, gh, None, reason)
            if pr.get("state") == "MERGED":
                self.finish(key, repo, base, pr, f"Pull request {pr['url']} is merged into {base}.", merged_pr=True)
                return True
            if not branch and pr.get("state") != "CLOSED":
                return self.block_gate(key, repo, base, gh, pr,
                                       "The ticket branch is not on origin, so there is no pull request to check.")
            if not branch:
                branch = str(pr.get("headRefName", ""))
            info = self.gate_facts(key, repo, gh, base, branch, pr)
        except (GitHubError, GitError, JiraError, ValueError, KeyError) as error:
            log(f"{key}: gate check failed: {error}")
            return False
        action, detail = decide(info)
        events.emit("gate.decision", ticket=key, action=action, detail=detail, pr=pr.get("number"), ci=info.get("ci"), behind=info.get("behind"),
                    conflicting=info.get("conflicting"), feedback=len(info.get("feedback", [])), stuck=len(info.get("stuck", [])),
                    qa_ok=info.get("qa_ok"), draft=info.get("draft"), waited_minutes=round(info.get("ci_waited_minutes", 0), 1))
        if action in ("qa", "merge") and not self.take_slot(key, repo):
            self.note_gate(key, "waiting for another pull request of this repository to finish QA and merge")
            return False
        self.redis.set(f"pr:{key}", str(pr["number"]), 30 * DAY)
        if action == "done":
            self.finish(key, repo, base, pr, f"Pull request {pr['url']} is merged into {base}.", merged_pr=True)
        elif action == "closed":
            repo.cleanup(key, remote=False)
            self.teardown_ticket_database(key)
            self.release_slot(key, repo.name)
            self.jira.comment(key, f"AI Flow: pull request {pr['url']} was closed without merging; declining the ticket.")
            self.jira.transition(key, "Declined")
        elif action == "update":
            terminal = self.gate_is_terminal(key, repo, base, gh, pr)
            if terminal is None:
                return False
            if terminal:
                return True
            return self.update_branch(key, gh, pr, base)
        elif action == "rework":
            terminal = self.gate_is_terminal(key, repo, base, gh, pr)
            if terminal is None:
                return False
            if terminal:
                return True
            self.rework_from_gate(key, repo, gh, pr, info, detail)
        elif action == "block":
            kind = "CI" if "CI" in detail else "Human"
            return self.block_gate(key, repo, base, gh, pr, f"{detail} ({pr['url']}).", kind)
        elif action == "wait":
            self.note_gate(key, f"{detail} ({pr['url']})")
            return False
        elif action == "ready":
            gh.ready(pr["number"])
            self.redis.set(f"readyat:{key}", str(int(time.time())), 30 * DAY)
            self.jira.comment(key, f"AI Flow: CI is green; {pr['url']} is ready for review. Waiting for the review bots, then QA.")
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
        ready = int(self.state(key, "readyat", "0"))
        info.update(ci_required=required, ci_wait_minutes=self.cfg.ci_wait_minutes, ci_waited_minutes=(time.time() - max(entry, pushed)) / 60,
                    draft=bool(pr.get("isDraft")), settled=time.time() - max(pushed, ready) >= self.cfg.feedback_settle_minutes * 60,
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
        if self.state(key, "isrelease"):
            return True  # a release always waits for the owner before it is merged and tagged
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
        agent, session_owner = self.reserve_agent("qa", key, view.get("agent"))
        if agent is None:
            self.unserved(key, "qa", view.get("agent"))
            self.release_slot(key, repo.name)
            return False
        try:
            try:
                workdir, _ = repo.ensure_worktree(key, view.get("summary", key), base)
                repo.sync_worktree(workdir, branch)
            except GitError as error:
                self.block(key, f"Cannot get the pull request head on {self.cfg.machine}: {error}", kind="Config")
                return True
            if not self.prepare_ticket_database(key, workdir):
                self.release_slot(key, repo.name)
                return True
            self.label_workspace(key)
            self.jira.comment(key, f"AI Flow: QA {agent} on {self.cfg.machine} started on {pr['url']} at {pr['headRefOid'][:10]} "
                                   f"(developer was {self.redis.get(f'dev:{key}') or '?'}).")
            why, text, found, quota = self.execute("qa", agent, key, view, workdir, branch, base, repo,
                                                   slot_owner=key, session_owner=session_owner)
        finally:
            if session_owner:
                self.release_agent_session(key, "qa", agent, session_owner)
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
            self.pr_comment(repo, pr["number"], f"### QA by {agent}: FAIL at {pr['headRefOid'][:10]}\n\n{text[-6000:]}")
            self.send_back(key, repo, f"QA {agent} FAILED the pull request: {found[1]}", text[-3000:], [])
            return True
        self.redis.set(f"qa:{key}", pr["headRefOid"], 30 * DAY)
        self.jira.comment(key, f"AI Flow: QA {agent} PASSED at {pr['headRefOid'][:10]}.\n\n{tail(text, 800)}")
        self.pr_comment(repo, pr["number"], f"### QA by {agent}: PASS at {pr['headRefOid'][:10]}\n\n{text[-6000:]}")
        return True

    def pr_comment(self, repo: Repo, number: int, body: str) -> None:
        """A checklist on the pull request itself (marked, so it is never read back as feedback); never fatal."""
        try:
            GitHub(repo.github_slug()).comment(number, body)
        except (GitHubError, GitError) as error:
            log(f"pull request #{number}: could not post the comment: {error}")

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
        self.finish(
            key, repo, base, gh.view(pr["number"]),
            f"Merged {pr['url']} into {base} ({self.cfg.project(key).merge_method}) after CI, feedback and {'the human approval' if approved else 'QA'}.",
            merged_pr=True,
        )
        return True

    # ---- Require human: Human review -> Ready to merge ------------------------------------------------------
    def to_human_review(self, key: str, repo: Repo, pr: dict[str, Any]) -> None:
        self.release_slot(key, repo.name)
        self.redis.set(f"humanreview:{key}", "1", 30 * DAY)
        events.emit("ticket.human_review", ticket=key, pr=pr.get("number"), url=pr.get("url"))
        self.jira.comment(key, f"AI Flow: CI, feedback and QA passed on {pr['url']} (head {pr['headRefOid'][:10]}). This ticket requires a human: "
                               f"review the pull request, then move the ticket to Ready to merge (the supervisor updates the branch if it is "
                               f"outdated and merges it) or to To Do with a comment to ask for changes. No preview deployment yet (JAIF-28).")
        self.jira.transition(key, "Human review")

    def declined(self, key: str, repo: Repo, pr: dict[str, Any]) -> None:
        repo.cleanup(key, remote=False)
        self.teardown_ticket_database(key)
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

    def finish(self, key: str, repo: Repo, base: str, pr: dict[str, Any], message: str, merged_pr: bool = False) -> None:
        cleanup_status = "Worktree and branch removed."
        step = "refresh base"
        defer_refresh = bool(self.cfg.project(key).deploy_command.strip())
        try:
            if not defer_refresh:
                repo.refresh_base(base)
            step = "cleanup"
            repo.cleanup(key, merged_pr=merged_pr)
        except GitError as error:
            log(f"{key}: {step}: {error}")
            cleanup_status = f"Cleanup failed ({error}); remove the worktree by hand."
        self.release_slot(key, repo.name)
        events.emit("ticket.done", ticket=key, repo=repo.name, base=base, pr=pr.get("number"), cleanup=cleanup_status[:120])
        self.jira.comment(key, f"AI Flow: {message} {cleanup_status}")
        self.jira.transition(key, "Done")
        self.memory_workflow.remember_case(key, repo, pr)
        self.clear_blocked_reason(key)
        self.teardown_ticket_database(key)
        self.after_merge(key, repo, base)
        self.jira.remove_label(key, "rework")
        for name in ("dev", "reviewer", "home", "cycles", "rework-history", "qa", "rework", "feedback", "seen", "pr", "prtitle", "pushed", "gateentry", "ishotfix",
                     "gatenote", "mergefail", "gatepoll", "repo", "humanreview", "humanok", "blocked", "devmodel", "recalled"):
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
            from .projects import desired_ids
            print("\n".join(desired_ids(config.registry, config.machine) if config.registry.from_files else [str(n) for n in range(1, config.workers + 1)]))
            return 0
        if "check" in args:  # the registry is loaded (and validated) above; show what was understood
            from .worker_scaling import configuration_summary
            summary, ids = configuration_summary(config)
            print(summary)
            limits = config.ai_limits
            print(f"  AI session limits: {', '.join(f'{n}={v}' for n, v in sorted(limits.items())) or 'none (all unlimited)'}; omitted AIs are unlimited")
            if not limits:
                print("  WARNING: no ai_limits: every worker may start AI sessions with no machine-wide cap (usage limits and cooldowns follow)")
            elif len(ids) > sum(limits.values()):
                print(f"  WARNING: {len(ids)} workers but the AI limits add up to {sum(limits.values())}: some workers will wait for an AI")
            obs = config.obs
            on = bool(obs.get("AIFLOW_OBS_URL") and obs.get("AIFLOW_OBS_USER") and obs.get("AIFLOW_OBS_PASSWORD"))
            print(f"  events: {'ON' if on else 'off'} -> {obs.get('AIFLOW_OBS_URL') or 'no obs_url'} org {obs.get('AIFLOW_OBS_ORG', 'default')} "
                  f"stream {obs.get('AIFLOW_OBS_STREAM', 'aiflow_events')}" + ("" if on else " (needs obs_url in aiflow.toml and AIFLOW_OBS_USER/PASSWORD in Workspaces/.env)"))
            memory = config.memory
            print(f"  memory: {'ON' if memory.get('AIFLOW_MEMORY_URL') and memory.get('AIFLOW_MEMORY_TOKEN') else 'off'} -> {memory.get('AIFLOW_MEMORY_URL') or 'no memory_url'}"
                  + ("" if memory.get('AIFLOW_MEMORY_TOKEN') else " (needs AIFLOW_MEMORY_TOKEN in Workspaces/.env)")
                  + ("" if memory.get('AIFLOW_MEMORY_AI_TOKEN') else "; the AIs get no aiflow-mem access without AIFLOW_MEMORY_AI_TOKEN"))
            print(operator_status.check_line(config, Redis, RedisError))
            for project in config.registry.projects.values():
                deploy_setting = (f"configured (timeout {project.deploy_timeout_minutes} min, on {project.deploy_on})"
                                  if project.deploy_command.strip() else "disabled")
                print(f"{project.jira_key}: repo {project.repo}, base {project.base}, hotfix {project.hotfix_base}, developer {project.developer or '-'}, "
                      f"reviewer {project.reviewer or '-'}, qa {project.qa or '-'}, human {project.require_human}, "
                      f"human on deployment {project.human_on_deployment}, auto-merge {project.auto_merge}, "
                      f"ci {project.ci_required}, max parallel {project.max_parallel or 'unlimited'}, workers {project.workers}, deploy {deploy_setting}")
            return 0
    except ConfigError as error:
        print(f"CONFIG ERROR: {error}", file=sys.stderr)
        return 2
    cfg = config
    redis = Redis(cfg.redis_host, cfg.redis_port, cfg.redis_db, cfg.redis_prefix, cfg.redis_password, cfg.redis_user)
    if "pause" in args or "resume" in args:  # workers finish their current step and then take no new work / take work again
        if "pause" in args:
            redis.set("paused", str(int(time.time())), 365 * DAY)
            print("paused: workers finish the current step and take no new ticket (aiflow-work resume to continue)")
        else:
            redis.delete("paused")
            print("resumed")
        return 0
    if "operator" in args:  # a separate switch of the operator (the policy file still has to enable it)
        if "off" in args:
            redis.set("operator:off", str(int(time.time())), 365 * DAY)
            print("operator switched off: no incident is handled (aiflow-work operator on to switch it on)")
        elif "on" in args:
            redis.delete("operator:off")
            print("operator switch on" + ("" if cfg.operator_policy.enabled else " (but config/operator.toml still has enabled = false)"))
        else:
            state = "off" if redis.get("operator:off") is not None else ("paused with the flow" if redis.get("paused") is not None else "on")
            print(f"operator: policy {'enabled' if cfg.operator_policy.enabled else 'disabled'}, switch {state}")
        return 0
    if "restart" in args:  # every worker leaves after its current step and systemd starts it again
        redis.set("restart", str(int(time.time())), 7 * 24 * 3600)  # workers started later ignore it
        print("restart requested for all workers")
        return 0
    events.configure(cfg.obs, cfg.machine, cfg.worker_label, cfg.root / ".tmp" / "aiflow", home=cfg.home)
    supervisor = Supervisor(cfg, Jira(cfg.root), redis)
    (cfg.root / ".tmp").mkdir(exist_ok=True)
    if "--once" in args:
        return 0 if supervisor.tick() else 3
    from .worker_scaling import run_worker
    run_worker(supervisor, redis, cfg)
    events.emit("worker.stop", retired=supervisor.retired)
    events.close()
    return 3 if supervisor.retired else 0  # 3: the unit does not restart it (RestartPreventExitStatus=2 3)


if __name__ == "__main__":
    raise SystemExit(main())
