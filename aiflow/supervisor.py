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
from .config import PROJECT_DIR, Settings, repo_listed
from .github import GitHub, GitHubError
from .gitops import GitError, Repo
from .jira import Jira, JiraError
from .lock import Redis

STAGES = {"To Do": "developer", "In Progress": "developer", "In Review": "reviewer", "In QA": "gate"}
DAY = 86400
SLOT_SECONDS = 600


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}", flush=True)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def ticket_field(description: str, name: str, default: str) -> str:
    match = re.search(rf"\b{name}:\s*([\w./-]+)", description or "")
    return match.group(1) if match else default


def decide(info: dict[str, Any]) -> tuple[str, str]:
    """What the pull-request gate does next. Pure: `info` holds facts only.

    Returns (action, detail) with action one of done, closed, update, rework, block, wait, qa, awaiting, merge."""
    if info["state"] == "MERGED":
        return "done", ""
    if info["state"] == "CLOSED":
        return "closed", ""
    if info["behind"]:
        return ("rework", "the branch conflicts with the base") if info["conflicting"] else ("update", "")
    if info["ci"] == "failed":
        return "rework", "CI failed"
    if info["ci"] == "pending":
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
    return ("merge", "") if info["auto_merge"] else ("awaiting", "")


class Supervisor:
    def __init__(self, cfg: Settings, jira: Jira, redis: Redis) -> None:
        self.cfg, self.jira, self.redis = cfg, jira, redis
        self.stop = False

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
        if status == "To Do":
            return all(b["status"] == "Done" for b in self.jira.blockers(key))
        if status == "In Progress":
            age = (datetime.now(timezone.utc) - parse_time(issue["updated"])).total_seconds()
            return age >= self.cfg.orphan_grace_seconds
        if status == "In QA":
            return not self.redis.get(f"gatepoll:{key}")
        return True

    def cooling(self, agent: str) -> bool:
        return self.redis.get(f"cooldown:{agent}") is not None

    def role_spec(self, role: str) -> tuple[str, str, str]:
        return agents.parse_spec(self.cfg.role_defaults.get(role, ""))

    def model_for(self, role: str, agent: str) -> tuple[str, str]:
        name, model, effort = self.role_spec(role)
        return (model, effort) if name == agent else ("", "")

    def pick_agent(self, role: str, key: str, field_agent: str | None) -> str | None:
        installed = agents.available(self.cfg.agent_order)
        present = [a for a in installed if not self.cooling(a)]
        preferred = self.role_spec(role)[0]
        if role == "developer":
            if field_agent in present:
                return field_agent
            if field_agent in installed:
                return present[0] if present else None
            if field_agent:
                return None  # the field names an AI this machine does not have; another machine may take it
            if preferred in present:
                return preferred
            return present[0] if present else None
        dev = self.redis.get(f"dev:{key}") or field_agent or ""
        reviewer = self.redis.get(f"reviewer:{key}")
        avoid = (reviewer,) if role == "qa" and reviewer else ()
        if preferred in present and preferred != dev and preferred not in avoid:
            return preferred
        return agents.choose_other(dev, present, avoid=avoid)

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
                view = self.jira.view(issue["key"])
                role = STAGES[issue["status"]]
                agent = None
                if role != "gate":
                    agent = self.pick_agent(role, issue["key"], view.get("agent"))
                    if agent is None:
                        self.note_waiting(issue["key"], role)
                        continue
                if not self.redis.acquire(f"lease:{issue['key']}", self.cfg.owner, self.cfg.lease_seconds):
                    continue
            except (JiraError, RuntimeError) as error:
                log(f"skip {issue.get('key')}: {error}")
                continue
            try:
                worked = self.process(issue, view, role, agent)
            finally:
                self.redis.release(f"lease:{issue['key']}", self.cfg.owner)
            if worked:
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
    def process(self, issue: dict[str, Any], view: dict[str, Any], role: str, agent: str | None) -> bool:
        """Returns True when something was done (so the loop does not sleep), False when the ticket only waits."""
        key = issue["key"]
        description = view.get("description", "")
        repo = Repo(self.cfg.root, ticket_field(description, "Repo", self.cfg.default_repo))
        base = ticket_field(description, "Base", self.cfg.default_base)
        if role != "gate":
            log(f"{key} [{issue['status']}] {role} -> {agent}")
        if not repo.exists():
            self.block(key, f"Repository `{repo.name}` was not found under JOOservices/projects.")
            return True
        if not repo.has_remote():
            self.block(key, f"Repository `{repo.name}` has no remote; the flow needs GitHub pull requests.")
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
        return self.gate(key, view, repo, base)

    def attempts(self, key: str, role: str) -> int:
        return int(self.redis.get(f"attempts:{key}:{role}") or 0)

    def block(self, key: str, reason: str) -> None:
        self.release_slot(key)
        self.jira.comment(key, f"AI Flow: blocked on {self.cfg.machine}. {reason}")
        self.jira.transition(key, "Blocked")

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
                      rework=self.rework_text(key), pr=self.pr_text(key), replies_file=str(self.replies_file(key)))
        prompt = "\n\n".join((PROJECT_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8") for name in ("common", role))
        for name, value in values.items():
            prompt = prompt.replace("{" + name + "}", str(value))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_dir = self.cfg.root / ".tmp" / "aiflow" / "logs"
        log_file, result_file = log_dir / f"{key}-{role}-{stamp}.log", log_dir / f"{key}-{role}-{stamp}.result"
        model, effort = self.model_for(role, agent)
        argv, stdin_text = agents.build_command(agent, workdir, result_file, prompt, self.cfg.root, model, effort)
        pushed_at = [time.monotonic()]

        def beat() -> bool:
            if role == "developer" and time.monotonic() - pushed_at[0] > 120:
                pushed_at[0] = time.monotonic()
                try:  # keep the branch on origin so another machine can continue after a crash
                    repo.push(workdir, branch)
                except GitError:
                    pass
            self.redis.set(f"hb:{self.cfg.machine}:{self.cfg.slot}", str(int(time.time())), 300)
            if slot_owner:
                self.redis.renew(f"qaslot:{repo.name}", slot_owner, SLOT_SECONDS)
            return self.redis.renew(f"lease:{key}", self.cfg.owner, self.cfg.lease_seconds)

        code, why = runner.run(
            argv, stdin_text, workdir, log_file, agents.child_env(),
            idle_seconds=self.cfg.idle_minutes * 60, max_seconds=self.cfg.max_minutes * 60, heartbeat_seconds=self.cfg.heartbeat_seconds,
            heartbeat=beat)
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

    def failed(self, key: str, role: str, agent: str, why: str, text: str) -> None:
        count = self.redis.incr(f"attempts:{key}:{role}", 7 * DAY)
        self.jira.comment(key, f"AI Flow: {agent} on {self.cfg.machine} ended the {role} run without a usable result ({why}); "
                               f"attempt {count}/{self.cfg.max_attempts}. Last output: {text[-600:]!r}")
        if count >= self.cfg.max_attempts:
            self.block(key, f"{self.cfg.max_attempts} failed {role} attempts; needs the owner.")

    # ---- developer -------------------------------------------------------------------------------------------
    def develop(self, key: str, issue: dict[str, Any], view: dict[str, Any], repo: Repo, base: str, agent: str) -> bool:
        if self.attempts(key, "developer") >= self.cfg.max_attempts:
            self.block(key, "Too many failed developer attempts.")
            return True
        if issue["status"] == "To Do" and not self.jira.transition(key, "In Progress"):
            log(f"{key}: could not start (someone else did?)")
            return False
        if view.get("agent") != agent:
            self.jira.set_agent(key, agent)
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
            self.block(key, f"{agent} reported: {found[1]}")
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
            title = github.fallback_title(key, view.get("summary", key), str(view.get("type", "")))
        self.redis.set(f"prtitle:{key}", github.with_key(title, key), 30 * DAY)
        try:
            repo.push(workdir, branch, lease=True)
        except GitError as error:
            self.failed(key, "developer", agent, f"push failed: {error}", text)
            return
        self.redis.set(f"pushed:{key}", str(int(time.time())), 30 * DAY)
        for name in ("attempts:%s:developer", "qa:%s"):
            self.redis.delete(name % key)
        if answers and self.post_replies(key, repo, answers):
            return
        self.jira.comment(key, f"AI Flow: {agent} finished on {self.cfg.machine}. Branch {branch} pushed to origin; sending to review.\n\n{text[-1500:]}")
        self.jira.transition(key, "In Review")

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
                            "Reasons are in the replies on the pull request.")
            return True
        return False

    # ---- reviewer --------------------------------------------------------------------------------------------
    def review(self, key: str, view: dict[str, Any], repo: Repo, base: str, agent: str) -> bool:
        if self.attempts(key, "reviewer") >= self.cfg.max_attempts:
            self.block(key, "Too many failed reviewer attempts.")
            return True
        try:
            workdir, branch = repo.ensure_worktree(key, view.get("summary", key), base)
            repo.sync_worktree(workdir, branch)
            repo.fetch(base)
        except GitError as error:
            self.block(key, f"Cannot get the ticket branch on {self.cfg.machine}: {error}")
            return True
        if repo.ahead(workdir, base) == 0:
            self.block(key, "The ticket branch has no commits ahead of the base; developer output is missing.")
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
        self.open_pull_request(key, view, repo, base, branch, agent, text)
        return True

    def open_pull_request(self, key: str, view: dict[str, Any], repo: Repo, base: str, branch: str, agent: str, text: str) -> None:
        gh = GitHub(repo.github_slug())
        title = self.state(key, "prtitle") or github.fallback_title(key, view.get("summary", key), str(view.get("type", "")))
        try:
            found = gh.find(branch)
            if found is None or found["state"] != "OPEN":
                body = (f"## Summary\n{view.get('summary', key)}\n\nTicket: {key}\n\n## Test plan\n"
                        f"- [x] Reviewed by {agent} (AI Flow)\n- [ ] CI green\n- [ ] QA on the pull request head\n\n{github.MARKER}")
                found = gh.create(branch, base, title, body)
        except GitHubError as error:
            self.failed(key, "reviewer", agent, f"could not open the pull request: {error}", text)
            return
        self.redis.set(f"pr:{key}", str(found["number"]), 30 * DAY)
        self.redis.set(f"pushed:{key}", self.state(key, "pushed", str(int(time.time()))), 30 * DAY)
        self.redis.set(f"gateentry:{key}", str(int(time.time())), 30 * DAY)
        self.jira.comment(key, f"AI Flow: reviewer {agent} PASSED. Pull request: {found['url']}. Waiting for CI, feedback and QA.\n\n{text[-800:]}")
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
            self.block(key, f"{cycles - 1} rework rounds without success; needs the owner.")
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
            self.block(key, "The ticket branch is not on origin, so there is no pull request to check.")
            return True
        gh = GitHub(repo.github_slug())
        try:
            pr = gh.find(branch)
            if pr is None:
                self.block(key, f"No pull request exists for branch {branch}.")
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
            self.finish(key, repo, pr, f"Pull request {pr['url']} is merged into {base}.")
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
            self.block(key, f"{detail} ({pr['url']}).")
        elif action == "wait":
            self.note_gate(key, f"{detail} ({pr['url']})")
            return False
        elif action == "qa":
            return self.run_qa(key, view, repo, base, branch, pr)
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
        required = repo_listed(self.cfg.ci_required_repos, repo.name)
        entry = int(self.state(key, "gateentry", str(int(time.time()))))
        pushed = int(self.state(key, "pushed", "0"))
        info.update(ci_required=required, ci_wait_minutes=self.cfg.ci_wait_minutes, ci_waited_minutes=(time.time() - max(entry, pushed)) / 60,
                    settled=time.time() - pushed >= self.cfg.feedback_settle_minutes * 60,
                    qa_ok=self.state(key, "qa") == pr["headRefOid"], auto_merge=repo_listed(self.cfg.auto_merge_repos, repo.name))
        if pr["state"] != "OPEN":
            return info
        info["behind"] = repo.behind(base, branch)
        info["conflicting"] = pr.get("mergeable") == "CONFLICTING"
        seen = set(json.loads(self.state(key, "seen", "[]")))
        found = gh.feedback(pr["number"], seen, self.cfg.ignored_authors)
        info["feedback"], info["stuck"] = found["items"], found["stuck"]
        return info

    def take_slot(self, key: str, repo: Repo) -> bool:
        """One pull request per repository is in QA/merge at a time, so a merge never makes the next QA stale twice."""
        name = f"qaslot:{repo.name}"
        return self.redis.acquire(name, key, SLOT_SECONDS) or self.redis.renew(name, key, SLOT_SECONDS)

    def note_gate(self, key: str, message: str) -> None:
        marker = f"gatenote:{key}"
        if self.redis.get(marker) != message:
            self.redis.set(marker, message, 30 * DAY)
            self.jira.comment(key, f"AI Flow: {message}.")

    def update_branch(self, key: str, gh: GitHub, pr: dict[str, Any], base: str) -> bool:
        try:
            gh.update_branch(pr["number"], pr["headRefOid"])
        except GitHubError as error:
            self.jira.comment(key, f"AI Flow: the branch is behind {base} and updating it failed ({error}); the next check retries.")
            return False
        self.redis.delete(f"qa:{key}")
        self.redis.set(f"pushed:{key}", str(int(time.time())), 30 * DAY)
        self.jira.comment(key, f"AI Flow: {base} moved, so {pr['url']} was updated with {base} (clean merge). CI and QA run again on the new head.")
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
            self.block(key, "Too many failed QA attempts.")
            return True
        agent = self.pick_agent("qa", key, view.get("agent"))
        if agent is None:
            self.note_waiting(key, "qa")
            self.release_slot(key, repo.name)
            return False
        try:
            workdir, _ = repo.ensure_worktree(key, view.get("summary", key), base)
            repo.sync_worktree(workdir, branch)
        except GitError as error:
            self.block(key, f"Cannot get the pull request head on {self.cfg.machine}: {error}")
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

    def merge(self, key: str, repo: Repo, gh: GitHub, pr: dict[str, Any], base: str) -> bool:
        try:
            fresh = gh.view(pr["number"])
            if fresh["headRefOid"] != self.state(key, "qa") or repo.behind(base, pr["headRefName"]):
                self.redis.delete(f"qa:{key}")  # something moved since QA: start over
                return True
            gh.merge(pr["number"], fresh["headRefOid"], self.cfg.merge_method)
        except (GitHubError, GitError) as error:
            count = self.redis.incr(f"mergefail:{key}", DAY)
            self.jira.comment(key, f"AI Flow: merging {pr['url']} failed ({count}/3): {str(error)[:400]}")
            if count >= 3:
                self.block(key, f"Merging {pr['url']} keeps failing; needs the owner.")
            return count >= 3
        self.finish(key, repo, gh.view(pr["number"]), f"Merged {pr['url']} into {base} ({self.cfg.merge_method}) after CI, feedback and QA.")
        return True

    def finish(self, key: str, repo: Repo, pr: dict[str, Any], message: str) -> None:
        try:
            repo.cleanup(key)
        except GitError as error:
            log(f"{key}: cleanup: {error}")
        self.release_slot(key, repo.name)
        self.jira.comment(key, f"AI Flow: {message} Worktree and branch removed.")
        self.jira.transition(key, "Done")
        self.jira.remove_label(key, "rework")
        for name in ("dev", "reviewer", "home", "cycles", "qa", "rework", "feedback", "seen", "pr", "prtitle", "pushed", "gateentry",
                     "gatenote", "mergefail", "gatepoll", "repo"):
            self.redis.delete(f"{name}:{key}")


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
