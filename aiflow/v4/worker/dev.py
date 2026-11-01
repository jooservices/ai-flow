"""The dev step: worktree from the latest base, AI run, then the worker commits, pushes and opens a draft PR.

Outcomes:
  done     changes committed and pushed, draft PR open, ticket in In Verify
  blocked  the AI reported a requirement conflict: ticket to Needs owner with the question
  auto_answered  the question was classified as one that never needs a person (docs/v4/escalations.md): ticket back to To Do with the answer
  tool_missing  a tool or service is missing on this machine: ticket back to To Do, parked for a capable machine
  quota    the account cannot work now: ticket back to To Do, account cooling down (control API)
  failed   anything else: ticket back to To Do with the reason (the branch keeps the work for the next run)
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters import RunResult, RunSpec, get_adapter
from ..core import commit_rules, handover, work_limits
from ..core.environment import items_of, parse_missing_environment, redact
from ..core.text import slug
from ..core.toolchains import (has_missing_marker, parse_command_not_found_requirements,
                               parse_missing_requirements)
from . import timeout_handover
from .client import Conflict, ControlClient
from . import gate_step
from .gitops import Git
from .notes import environment_notes, environment_status, note, project_env
from .run_steps import (REPO_ROOT as REPO_ROOT, RunReporter, adapter_for_run, api_node,
                        build_prompt as _build_prompt, classify_question, commit_message, fetch_ci_log,
                        machine_block as machine_block, make_run_spec, pr_title, project_environment,
                        related_block as related_block,
                        run_reporter, run_with_inbox, runtime_for_run, start_run, stop_heartbeat,
                        with_knowledge_context as with_knowledge_context, worktree_path)
from .wordpress import declares_wordpress, start_site as start_wordpress_site

log = logging.getLogger("aiflow4.dev")
SUMMARY = re.compile(r"^SUMMARY:\s*(.+)$", re.M)
# a real conflict marker starts a line ("<<<<<<< HEAD"); the same characters inside code or docs are not one
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)|^={7}$", re.M)
TESTS = re.compile(r"^TESTS:\s*(.+)$", re.M)
PROVIDER_NAME = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "opencode": "OpenCode"}


@dataclass
class Claimed:
    run_id: str
    fence: int
    run_token: str
    account: str
    provider: str
    model: str
    effort: str
    ticket: dict
    project: dict
    identity: tuple[str, str]
    step: str = "dev"
    findings: str = ""
    branch: str = ""
    pr: dict | None = None
    last_dev_summary: str = ""
    verify_findings: str = ""
    recent_comments: list[dict] | list[str] | None = None
    triage_answer: str = ""
    triage_question: str = ""
    related: list[dict] = field(default_factory=list)
    project_tickets: list[dict] = field(default_factory=list)
    context_pack: dict = field(default_factory=dict)
    machine_tools: dict = field(default_factory=dict)
    owner_register: list[dict] = field(default_factory=list)
    escalation_class: str = ""


class LeaseLost(Exception):
    pass


class Heartbeat(threading.Thread):
    def __init__(self, api: ControlClient, run: Claimed, every_s: int) -> None:
        super().__init__(daemon=True, name=f"hb-{run.ticket['key']}")
        self.api, self.run_, self.every = api, run, every_s
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.progress: dict = {}
        self.inbox: dict[int, dict] = {}
        self.inbox_lock = threading.Lock()
        self.included_ids: set[int] = set()
        self.pending_received: set[int] = set()
        self.pending_included: set[int] = set()

    def _poll(self) -> list[dict]:
        with self.inbox_lock:
            received = sorted(self.pending_received)
            included = sorted(self.pending_included)
        response = self.api.call("POST", f"/v1/runs/{self.run_.run_id}/heartbeat",
                                 {"fence": self.run_.fence, "progress": self.progress,
                                  "inbox_received": received, "inbox_included": included})
        with self.inbox_lock:
            for message_id in received:
                self.pending_received.discard(message_id)
            for message_id in included:
                self.pending_included.discard(message_id)
                self.inbox.pop(message_id, None)
            for message in response.get("inbox") or []:
                message_id = int(message["id"])
                if message_id not in self.included_ids:
                    self.inbox[message_id] = message
            return sorted(self.inbox.values(), key=lambda message: int(message["id"]))

    def include(self, messages: list[dict]) -> None:
        """Acknowledge inclusion only after the provider process starts with these messages in its prompt."""
        message_ids = {int(message["id"]) for message in messages}
        with self.inbox_lock:
            self.included_ids.update(message_ids)
            self.pending_included.update(message_ids)
            for message_id in message_ids:
                self.inbox.pop(message_id, None)
        try:
            self._poll()
        except Conflict:
            self.lost.set()
        except Exception as e:  # noqa: BLE001
            log.warning("run inbox acknowledgement failed: %s", e)

    def checkpoint(self, max_messages: int = 10, max_chars: int = 20000) -> list[dict]:
        """Fetch the inbox at a provider-turn boundary and acknowledge only a bounded prompt batch."""
        try:
            self._poll()
            with self.inbox_lock:
                pending = sorted(self.inbox.values(), key=lambda message: int(message["id"]))
            batch, used = [], 0
            for message in pending:
                size = len(str(message.get("body") or ""))
                if batch and (len(batch) >= max_messages or used + size > max_chars):
                    break
                batch.append(message)
                used += size
            if not batch:
                return []
            with self.inbox_lock:
                self.pending_received.update(int(message["id"]) for message in batch)
            self._poll()
            return batch
        except Conflict:
            self.lost.set()
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("run inbox checkpoint failed: %s", e)
            return []

    def run(self) -> None:
        while not self.stop.wait(self.every):
            try:
                self._poll()
            except Conflict:
                self.lost.set()
                return
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)


def build_prompt(c: Claimed, branch: str, conflicts: list[str] | None = None, ci_log: str = "",
                 env: dict[str, str] | None = None) -> str:
    return _build_prompt(c, branch, conflicts, ci_log, env, project_env)


@dataclass
class DevRun:
    api: ControlClient
    claim: Claimed
    srv_root: str
    control_url: str
    run_as: list[str]
    key: str
    project: dict
    repo: str
    branch: str
    git: Git
    heartbeat: Heartbeat
    started_at: float
    reporter: RunReporter
    result: dict
    worktree: Path | None = None
    branch_commit_count: int = 0
    conflicts: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    ci_log: str = ""
    response: RunResult | None = None
    summary: str = ""
    tests: str = ""
    handed: dict = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)


@dataclass
class DevBrief:
    spec: RunSpec
    adapter: Any
    runtime: Any
    wordpress: bool
    on_progress: Any


@dataclass
class DevProgress:
    heartbeat: Heartbeat
    started_at: float
    events: int = 0

    def __call__(self, event: dict) -> None:
        self.events += 1
        self.heartbeat.progress = {"phase": "ai", "events": self.events,
                                   "last": str(event.get("type", ""))[:40],
                                   "elapsed_s": int(time.time() - self.started_at)}


def prepare(run: DevRun) -> None:
    """Prepare the branch, worktree and environment, then record the started ticket state."""
    c, project = run.claim, run.project
    credential = run.api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                              "repo": run.repo})
    mirror = run.git.mirror(run.repo, credential["token"], credential.get("transport_url", ""))
    wt = worktree_path(run.srv_root, project["key"], run.branch)
    run.worktree = wt
    moved = run.git.foreign_worktree(mirror, wt, run.branch)
    if moved:
        report_foreign_worktree(run, moved, wt)
    start = run.git.worktree(mirror, wt, run.branch, project["base"])
    run.branch_commit_count = (run.git.commits_ahead(wt, project["base"])
                               if start == f"origin/{run.branch}" else 0)
    if start != f"origin/{project['base']}":
        run.conflicts = run.git.merge_base_in(wt, project["base"], c.identity)
    run.heartbeat.progress = {"phase": "worktree", "start": start, "conflicts": len(run.conflicts)}
    if moved:
        run.heartbeat.progress["repo_change"] = f"{moved['old_repo']} -> {run.repo}"
    run.env = project_environment(project["key"], project, "dev", project_env)
    report_started(run, start)
    run.ci_log = fetch_ci_log(run.api, c, run.branch)


def report_foreign_worktree(run: DevRun, moved: dict, worktree: Path) -> None:
    key, branch, repo = run.key, run.branch, run.repo
    log.warning("%s: repository changed from %s to %s; replacing worktree %s", key, moved["old_repo"], repo, worktree)
    work = (f"{len(moved['commits'])} committed change(s) on branch {branch} in {moved['old_repo']}"
            + (" (the branch is also pushed there):\n" if moved["pushed"] else " (not pushed anywhere):\n")
            + "\n".join(f"- {item}" for item in moved["commits"][:20])) if moved["commits"] else "no committed work on the old branch"
    run.reporter.jira("comment", body=note(
        run.claim, "repository changed", what=f"The ticket's repository changed from {moved['old_repo']} to {repo}",
        why="The old worktree belongs to the old repository, so the AI could not find the code in it",
        result=f"Old worktree and branch {branch} removed; {work}",
        next_=f"A new worktree is created from {repo} {run.project['base']}", node=api_node(run.api)))
    run.git.drop_foreign_worktree(worktree, branch, Path(moved["old_mirror"]))


def report_started(run: DevRun, start: str) -> None:
    c, project = run.claim, run.project
    run.reporter.jira("transition", to="In Progress")
    run.reporter.jira("fields", fields={"Workspace": environment_status(project.get("environment"), "dev", run.env),
                                        "Agent (AI)": {"value": PROVIDER_NAME.get(c.provider, c.provider)}})
    what = f"Develop the ticket on branch {run.branch} from {start}"
    if run.conflicts:
        what += f", with the latest {project['base']} merged in ({len(run.conflicts)} conflicted files to resolve)"
    run.reporter.jira("comment", body=note(
        c, "started", what=what,
        why=("Rework after verify findings" if c.findings else "The ticket is ready and its blockers are done"),
        next_="The AI implements the acceptance criteria and runs the checks; AI Flow commits and opens a draft PR",
        node=api_node(run.api)))


def brief(run: DevRun) -> DevBrief:
    """Assemble the run prompt, log path, provider request and execution runtime."""
    logs = Path(run.srv_root) / "logs" / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    run.env = project_environment(run.project["key"], run.project, "dev", project_env)
    spec = make_run_spec(build_prompt(run.claim, run.branch, run.conflicts, run.ci_log, run.env),
                         run.worktree, run.srv_root, run.claim, run.project, run.control_url,
                         logs / f"{run.key}-{run.claim.run_id}.jsonl", run.env)
    adapter = adapter_for_run(run.claim.provider, run.run_as, get_adapter)
    wordpress = declares_wordpress(run.project.get("stacks") or run.project.get("stack"))
    runtime = runtime_for_run(spec, run.run_as, run.project, wordpress)
    return DevBrief(spec, adapter, runtime, wordpress, DevProgress(run.heartbeat, run.started_at))


def run_ai(run: DevRun, prepared: DevBrief) -> None:
    """Run the provider inside the configured runtime, starting WordPress first when requested."""
    with prepared.runtime as containers:
        if prepared.wordpress:
            run.heartbeat.progress = {"phase": "wordpress-site"}
            start_wordpress_site(containers, run.worktree, run.project, run.claim.run_id, prepared.spec)
            run.reporter.jira("comment", body=note(
                run.claim, "WordPress test site ready", what="A temporary WordPress test site is ready",
                result="WP_BASE_URL (provided), WP_ADMIN_USER (provided), and WP_ADMIN_PASSWORD (provided) are set in the run environment",
                next_="Use `npx playwright test` with the project config; Chromium, Firefox, and WebKit are installed",
                node=api_node(run.api)))
        run.response = run_with_inbox(prepared.adapter, prepared.spec, run.heartbeat, prepared.on_progress)


def collect(run: DevRun) -> dict | None:
    """Collect provider details, handover metadata, missing requirements and changed files."""
    response = run.response
    record_response(run)
    if run.heartbeat.lost.is_set():
        raise LeaseLost()
    collect_summary(run)
    outcome = collect_quota(run)
    if outcome is not None:
        return outcome
    outcome = collect_environment(run)
    if outcome is not None:
        return outcome
    outcome = collect_missing_tools(run)
    if outcome is not None:
        return outcome
    outcome = collect_blocked(run)
    if outcome is not None:
        return outcome
    run.changed = run.git.changed(run.worktree)
    run.result.setdefault("details", {})["changed_files"] = run.changed[:100]
    if response.error_kind == "timeout" and timeout_handover.try_handover(
            run.api, run.claim, run.git, run.worktree, run.branch, run.repo, run.summary, run.handed,
            run.result, run.started_at, run.reporter.jira, run.branch_commit_count, run.tests):
        return run.result
    mark_unresolved_conflicts(run)
    return None


def record_response(run: DevRun) -> None:
    response = run.response
    run.result.update(tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=response.cost_usd,
                      quota=response.quota,
                      quota_observed_at=(getattr(response, "quota_observed_at", None).isoformat()
                                         if getattr(response, "quota_observed_at", None) else None),
                      error_kind=response.error_kind,
                      reset_at=response.reset_at.isoformat() if response.reset_at else None,
                      details={"verdict": response.verdict, "permission_denials": response.permission_denials,
                               "token_usage_reported": bool(getattr(response, "token_usage_reported", False)),
                               "verdict_reason": (response.verdict_reason or "")[:2000],
                               "exit_code": response.exit_code, "duration_s": int(response.duration_s),
                               "log": response.log_path})


def collect_summary(run: DevRun) -> None:
    response, c = run.response, run.claim
    summary_match = SUMMARY.search(response.final_text or "")
    tests_match = TESTS.search(response.final_text or "")
    run.summary = (summary_match.group(1).strip() if summary_match else
                   ((response.final_text or "").strip()[-600:] or response.error_message))
    run.tests = tests_match.group(1).strip() if tests_match else ""
    run.result["summary"] = run.summary
    run.handed = handover.parse(response.final_text or "", c.ticket.get("description") or "")
    run.result.setdefault("details", {}).update(run.handed)


def collect_quota(run: DevRun) -> dict | None:
    response = run.response
    if response.error_kind not in ("quota", "auth"):
        return None
    run.result["outcome"] = "quota"
    run.reporter.jira("comment", body=note(
        run.claim, "paused", what="The run stopped before finishing",
        why=f"{run.claim.account} cannot work now ({response.error_kind}): {response.error_message[:200]}",
        result="No change was pushed", next_="Back to To Do for another account; this account pauses until its limit resets",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def collect_environment(run: DevRun) -> dict | None:
    response = run.response
    names, _other, marked = parse_missing_environment(
        response.verdict_reason, items_of(run.project.get("environment"))) if response.verdict == "BLOCKED" else ([], [], False)
    if not marked:
        return None
    if not names:
        reason = ("unaccepted missing environment report: name an item of the project's environment list, "
                  f"not free text ({response.verdict_reason})")[:1000]
        run.result.update(outcome="failed", summary=reason)
        report_check_failure(run, reason, (response.final_text or run.summary)[:600])
        return run.result
    run.result["outcome"] = "environment_missing"
    run.result["details"]["missing_environment"] = names
    run.reporter.jira("comment", body=note(
        run.claim, "waiting for an environment item",
        what="The run needs an item of the project environment that is not provided",
        why=f"Missing environment item(s): {', '.join(names)}",
        result="No question goes to the owner; the item is recorded by name",
        next_="Back to To Do; AI Flow provides the item or raises one environment ticket",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def report_check_failure(run: DevRun, reason: str, result: str) -> None:
    run.reporter.jira("comment", body=note(
        run.claim, "check could not run", what="The run ended without a change AI Flow can push",
        why=reason[:600], result=result, next_="Back to To Do; the next run retries after a short wait",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")


def collect_missing_tools(run: DevRun) -> dict | None:
    response = run.response
    if response.verdict != "BLOCKED":
        return None
    missing, commands = missing_tools(run)
    if has_missing_marker(response.verdict_reason) and not missing:
        reason = f"unaccepted missing requirement report: {response.verdict_reason}"[:1000]
        run.result.update(outcome="failed", summary=reason)
        report_check_failure(run, reason, (response.final_text or run.summary)[:600])
        return run.result
    if not missing:
        return None
    raised = raise_missing_tool(run, commands)
    if raised.get("resolved") and raised.get("outcome") == "auto_answered":
        return report_auto_answer(run, raised, missing_tools=True)
    return report_missing_tools(run, raised, missing, commands)


def missing_tools(run: DevRun) -> tuple[list[str], list[str]]:
    response = run.response
    missing = parse_missing_requirements(response.verdict_reason)
    commands = parse_command_not_found_requirements(getattr(response, "command_results", []))
    missing = list(dict.fromkeys(missing + commands))
    machine = run.claim.machine_tools or {}
    available_tools, available_services = set(machine.get("available") or {}), set(machine.get("services") or [])
    return [name for name in missing if name not in available_tools and name not in available_services], commands


def raise_missing_tool(run: DevRun, commands: list[str]) -> dict:
    response, c = run.response, run.claim
    question = response.verdict_reason
    if commands and not has_missing_marker(response.verdict_reason or ""):
        question = f"MISSING-TOOL: {', '.join(commands)} - executed command returned command not found"
    return run.api.call("POST", "/v1/escalations/raise", {
        "run_id": c.run_id, "fence": c.fence, "question": question})


def report_auto_answer(run: DevRun, raised: dict, missing_tools: bool = False) -> dict:
    run.result["outcome"] = (raised.get("outcome", "auto_answered") if missing_tools else raised["outcome"])
    if missing_tools:
        run.result["details"]["escalation_class"] = raised.get("class", "")
        run.result["details"]["missing_tools"] = []
    else:
        if raised.get("missing_tools"):
            run.result["details"]["missing_tools"] = raised["missing_tools"]
        run.result["details"]["escalation_class"] = raised["class"]
    cited = f" {redact(raised['citation'])}." if raised.get("citation") else ""
    label = raised.get("label", "Resolved automatically") if missing_tools else raised["label"]
    reason = raised.get("reason", "the register answered this") if missing_tools else raised["reason"]
    answer = raised.get("answer", "") if missing_tools else raised["answer"]
    run.reporter.jira("comment", body=note(
        run.claim, "question answered without the owner",
        what="The developer AI stopped with a question that AI Flow answered automatically",
        why=redact(f"Classified as: {label} ({reason}).{cited}"), result=redact(answer),
        next_="Back to To Do; the next run gets this answer in its prompt",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def report_missing_tools(run: DevRun, raised: dict, missing: list[str], commands: list[str]) -> dict:
    response = run.response
    run.result["outcome"] = "tool_missing"
    run.result["details"]["missing_tools"] = missing
    if commands:
        run.result["details"]["command_not_found_tools"] = commands
    run.result["details"]["tool_missing_evidence"] = (
        "explicit_marker" if parse_missing_requirements(response.verdict_reason or "") else "command_not_found")
    run.result["details"]["escalation_class"] = raised["class"]
    run.reporter.jira("comment", body=note(
        run.claim, "waiting for a check tool", what="A configured check could not run",
        why=f"Missing tool(s): {', '.join(missing)}. {redact(response.verdict_reason)[:300]}",
        result="No question goes to the owner; the missing tool is recorded by name",
        next_="Back to To Do; AI Flow offers it again when a worker with the tool is available",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def collect_blocked(run: DevRun) -> dict | None:
    response = run.response
    if response.verdict != "BLOCKED":
        return None
    raised = classify_question(run.api, run.claim, response.verdict_reason)
    if raised and raised.get("resolved"):
        return report_auto_answer(run, raised)
    label = f" [{raised['label']}]" if raised and raised.get("label") else ""
    run.result["outcome"] = "blocked"
    run.reporter.jira("comment", body=note(
        run.claim, "question for the owner", what="The developer AI stopped before changing code",
        why=response.verdict_reason + label, next_="Answer in a comment and move the ticket back to To Do",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="Needs owner")
    return run.result


def mark_unresolved_conflicts(run: DevRun) -> None:
    if not run.conflicts:
        return
    unresolved = any(CONFLICT_MARKER.search((run.worktree / name).read_text(errors="ignore"))
                     for name in run.conflicts if (run.worktree / name).is_file())
    if unresolved:
        run.response.error_kind = run.response.error_kind or "unresolved conflicts"


def publish(run: DevRun) -> dict:
    """Choose the no-work or failure report, otherwise commit, gate, push and open the draft PR."""
    response = run.response
    failure_verdict = response.verdict == "FAIL"
    if response.error_kind or failure_verdict or not run.changed:
        clean_no_changes = not failure_verdict and not response.error_kind and not run.changed
        reason = response.error_kind or ((response.verdict_reason or "AI reported RESULT: FAIL")
                                         if failure_verdict else "no changes")
        if clean_no_changes:
            return publish_no_changes(run)
        return publish_failure(run, reason)
    problems = commit_rules.message_violations(commit_message(run.claim, run.summary))
    problems += commit_rules.violations(pr_title(run.claim.ticket), pr_title=True)
    if problems:
        return publish_invalid_title(run, problems)
    return publish_changes(run)


def publish_no_changes(run: DevRun) -> dict:
    c = run.claim
    found = run.api.call("GET", "/v1/github/pr", params={"run_id": c.run_id, "fence": c.fence,
                                                           "repo": run.repo, "head": run.branch})
    existing_pr = found if found and found.get("found") else None
    if not existing_pr and not run.branch_commit_count:
        return publish_no_work(run)
    return publish_existing_work(run, existing_pr)


def publish_existing_work(run: DevRun, existing_pr: dict | None) -> dict:
    c, wt = run.claim, run.worktree
    pushed = False
    if run.branch_commit_count:
        credential = run.api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                                  "repo": run.repo, "branch": run.branch})
        run.git.push(wt, run.branch, credential["token"])
        pushed = True
    sha = (run.git.run("rev-parse", "HEAD", cwd=wt).strip() if pushed or not existing_pr
           else existing_pr.get("head_sha") or run.git.run("rev-parse", "HEAD", cwd=wt).strip())
    pr = existing_pr or open_existing_work_pr(run)
    note_text = (f"The existing pull request already contains the ticket work: {pr['url']}" if existing_pr else
                 f"The ticket branch already contains {run.branch_commit_count} committed change(s); "
                 f"AI Flow opened a draft pull request: {pr['url']}")
    summary_done = f"Already done: no new changes were needed; {note_text}."
    run.result.setdefault("details", {}).update(already_done=True, no_changes=True,
                                                 branch_commits=run.branch_commit_count)
    run.result.update(outcome="done", summary=summary_done[:1000], pr_url=pr["url"], head_sha=sha)
    run.reporter.jira("comment", body=note(
        c, "already done; sent to verification", what="The developer found the work already on the ticket branch",
        result=note_text, next_="Another account verifies the pull request against the acceptance criteria",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="In Verify")
    return run.result


def open_existing_work_pr(run: DevRun) -> dict:
    c, project = run.claim, run.project
    pr_body = (f"Jira: {run.key}\n\n## Summary\nThe existing ticket branch already contains the work; "
               "this developer run made no additional changes.\n\n"
               f"## Tests\n{run.tests or 'see the existing branch history'}\n\n"
               f"{handover.pr_sections(run.handed)}"
               "## Run\n"
               f"- account: {c.account} ({c.provider}{'/' + c.model if c.model else ''})\n"
               f"- run: {c.run_id}, fence {c.fence}\n"
               f"- existing branch commits: {run.branch_commit_count}\n\n"
               "Opened as a draft by AI Flow v4; verification runs next (In Verify).")
    return run.api.call("POST", "/v1/github/pr", {
        "run_id": c.run_id, "fence": c.fence, "repo": run.repo, "head": run.branch,
        "base": project["base"], "title": pr_title(c.ticket), "body": pr_body, "draft": True,
    })


def publish_no_work(run: DevRun) -> dict:
    reason = (f"no changes to verify: branch {run.branch} has no committed work for this ticket and no open pull request")
    run.result.update(outcome="failed", error_kind="no_work", summary=reason[:1000])
    run.result.setdefault("details", {}).update(no_changes=True, no_work_reason=reason)
    run.reporter.jira("comment", body=note(
        run.claim, "no work to verify", what="The developer found no changes and the ticket branch has no committed work",
        why=reason, result=run.summary[:600] if run.summary else "The developer reported no changes needed",
        next_="The flow doctor records the cause and decides whether to retry after the normal backoff",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def publish_failure(run: DevRun, reason: str) -> dict:
    run.result["outcome"] = work_limits.run_outcome_for_error(reason)
    if reason == "timeout":
        run.result["error_kind"] = "timeout"
    run.result["summary"] = f"{reason}: {run.summary}"[:1000]
    run.reporter.jira("comment", body=note(
        run.claim, "no usable change", what="The run ended without a change AI Flow can push",
        why=reason, result=(run.summary[:600] if run.summary else "no output"),
        next_="Back to To Do; the next run retries after a short wait",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def publish_invalid_title(run: DevRun, problems: list[str]) -> dict:
    reason = "invalid title or commit message: " + "; ".join(problems)
    run.result.update(outcome="failed", error_kind="invalid_title", summary=reason[:1000])
    run.reporter.jira("comment", body=note(
        run.claim, "invalid title", what="The commit message or title breaks the repository rules",
        why=reason[:400], next_="Back to To Do; nothing was committed or pushed",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def publish_changes(run: DevRun) -> dict:
    run.heartbeat.progress = {"phase": "commit", "files": len(run.changed)}
    message = commit_message(run.claim, run.summary)
    sha = run.git.commit_all(run.worktree, message, run.claim.identity)
    gate_note = run_local_gate(run)
    if isinstance(gate_note, dict):
        return gate_note
    credential = run.api.call("POST", "/v1/github/token", {"run_id": run.claim.run_id,
                                                              "fence": run.claim.fence, "repo": run.repo,
                                                              "branch": run.branch})
    run.git.push(run.worktree, run.branch, credential["token"])
    pr = open_changes_pr(run, gate_note)
    report_published(run, sha, pr)
    run.result.update(outcome="done", pr_url=pr["url"], head_sha=sha)
    return run.result


def run_local_gate(run: DevRun) -> str | dict:
    if not gate_step.enabled(run.project):
        return ""
    run.heartbeat.progress = {"phase": "gate"}
    try:
        gate = gate_step.run(run.worktree, run.project, pr_title(run.claim.ticket), run.env, run.run_as)
        reason = gate_step.failure_text(gate) if gate.failed else ""
        error_kind = "gate_failed"
    except Exception as exc:  # noqa: BLE001 - a gate that cannot run must not leave an unpushed commit behind
        gate, reason, error_kind = None, f"the gate could not run: {type(exc).__name__}: {exc}", "gate_error"
    if gate is None or gate.failed:
        return report_gate_failure(run, reason, error_kind)
    return gate_step.pr_note(gate)


def report_gate_failure(run: DevRun, reason: str, error_kind: str) -> dict:
    saved = gate_step.keep_failed_work(
        run.git, run.worktree, Path(run.srv_root) / "logs" / "runs" / f"{run.key}-{run.claim.run_id}")
    run.git.run("reset", "--soft", "HEAD~1", cwd=run.worktree)
    run.result.update(outcome="failed", error_kind=error_kind, summary=reason[:1000])
    run.reporter.jira("comment", body=note(
        run.claim, "local gate failed", what="The CI checks fail on the local commit",
        why=redact(reason)[:900], next_=f"Back to To Do; nothing was pushed; the failed work is saved as {saved}",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="To Do")
    return run.result


def open_changes_pr(run: DevRun, gate_note: str) -> dict:
    c = run.claim
    body = (f"Jira: {run.key}\n\n## Summary\n{run.summary}\n\n## Tests\n{run.tests or 'see the run log'}\n\n"
            f"{handover.pr_sections(run.handed)}{gate_note}"
            f"## Run\n- account: {c.account} ({c.provider}{'/' + c.model if c.model else ''})\n"
            f"- run: {c.run_id}, fence {c.fence}\n- files changed: {len(run.changed)}\n\n"
            "Opened as a draft by AI Flow v4; verification runs next (In Verify).")
    return run.api.call("POST", "/v1/github/pr", {
        "run_id": c.run_id, "fence": c.fence, "repo": run.repo, "head": run.branch,
        "base": run.project["base"], "title": pr_title(c.ticket), "body": body, "draft": True,
    })


def report_published(run: DevRun, sha: str, pr: dict) -> None:
    run.reporter.jira("comment", body=note(
        run.claim, "draft PR opened", what=run.summary[:700],
        result=f"{len(run.changed)} files committed as {sha[:10]} on {run.branch}; draft PR {pr['url']}. Tests: {run.tests or 'not reported'}",
        next_="Another account verifies the PR against the acceptance criteria and merges it",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.jira("transition", to="In Verify")


def run_dev(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
            run_as: list[str]) -> dict:
    key, project = c.ticket["key"], c.project
    repo = project["repo"]
    branch = c.branch or f"{key}-{slug(c.ticket['summary'])}"
    started = start_run(api, c, srv_root, run_as, heartbeat_s, Git, Heartbeat)
    result = {"fence": c.fence, "outcome": "failed", "branch": branch}
    run = DevRun(api, c, srv_root, control_url, run_as, key, project, repo, branch, started.git,
                 started.heartbeat, started.started_at, run_reporter(api, c), result)
    try:
        prepare(run)
        prepared = brief(run)
        run_ai(run, prepared)
        outcome = collect(run)
        return outcome if outcome is not None else publish(run)
    except LeaseLost:
        result.update(outcome="lost", summary="lease lost during the run; stopped without writing")
        return result
    except Conflict:
        result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("dev step failed")
        result.update(outcome="failed", summary=f"worker error: {e}"[:1000])
        try:
            run.reporter.jira("comment", body=note(
                c, "worker error", what="The worker failed before the AI could finish", why=str(e)[:400],
                next_="Back to To Do; the next run retries after a short wait",
                t0=started.started_at, node=api_node(api)))
            run.reporter.jira("transition", to="To Do")
        except Exception:  # noqa: BLE001
            pass
        return result
    finally:
        stop_heartbeat(started.heartbeat)
