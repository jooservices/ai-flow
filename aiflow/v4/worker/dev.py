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
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters import RunSpec, get_adapter
from ..core import handover
from ..core.environment import items_of, parse_missing_environment, redact
from ..core.work_limits import FULL_SUITE_TIME_SLICE_MINUTES
from ..core.text import slug
from ..core.toolchains import (has_missing_marker, parse_command_not_found_requirements,
                               parse_missing_requirements)
from .client import Conflict, ControlClient
from .gitops import Git
from .notes import environment_notes, environment_status, note, project_env
from .wordpress import declares_wordpress, start_site as start_wordpress_site

log = logging.getLogger("aiflow4.dev")
REPO_ROOT = Path(__file__).resolve().parents[3]
SUMMARY = re.compile(r"^SUMMARY:\s*(.+)$", re.M)
# a real conflict marker starts a line ("<<<<<<< HEAD"); the same characters inside code or docs are not one
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)|^={7}$", re.M)
TESTS = re.compile(r"^TESTS:\s*(.+)$", re.M)
FULL_SUITE = re.compile(r"^FULL SUITE:\s*(FINISHED|UNFINISHED)(?:\s*[-:]\s*(.*))?$", re.M | re.I)
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

    def run(self) -> None:
        while not self.stop.wait(self.every):
            try:
                self.api.call("POST", f"/v1/runs/{self.run_.run_id}/heartbeat",
                              {"fence": self.run_.fence, "progress": self.progress})
            except Conflict:
                self.lost.set()
                return
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)


def build_prompt(c: Claimed, branch: str, conflicts: list[str] | None = None, ci_log: str = "",
                 env: dict[str, str] | None = None) -> str:
    tpl = (REPO_ROOT / "prompts" / "v4" / "dev.md").read_text(encoding="utf-8")
    checks = "\n".join(f"  - {x}" for x in c.project.get("checks") or []) or "  - the project's test suite"
    if c.project.get("containers"):
        checks += "\n  - Docker commands go through the run's rootless, worktree-confined runtime"
    timeout_retries = int(c.project.get("timeout_retries") or 0)
    retry_context = (f"This ticket has timed out {timeout_retries} time(s); the flow increased this run's hard limit "
                     f"to {int((c.project.get('work_minutes') or {}).get('hard', 60))} minutes. Reassess and split the work "
                     "if needed; do not repeat the same run plan." if timeout_retries else "")
    t = c.ticket
    rework = ""
    if c.findings:
        rework += ("\nThis is a rework: the branch already holds your earlier work and a reviewer sent it back. Fix every"
                   " finding below, keep what was right, and run the checks again. Before you change code, list each finding"
                   " with its number and, for each, ask whether the same kind of defect exists elsewhere in the change;"
                   " fix those too. Then update your AC-MAP and SELF-REVIEW.\nFindings:\n" + c.findings.strip() + "\n")
    if c.triage_answer:
        rework += ("\nAn earlier run on this ticket stopped with a question and the master AI answered it. Use this"
                   " answer and continue; do not stop on the same question again.\n"
                   + (f"Question:\n{c.triage_question.strip()}\n" if c.triage_question else "")
                   + "Answer:\n" + c.triage_answer.strip() + "\n")
    if ci_log:
        rework += ("\nThe CI of this branch's pull request failed. This is the tail of the failing job's log, read through"
                   " the control API; never ask for the CI log:\n" + ci_log.strip() + "\n")
    if conflicts:
        rework += ("\nThe latest " + c.project["base"] + " was merged into the branch and these files have conflict"
                   " markers (<<<<<<<, =======, >>>>>>>): " + ", ".join(conflicts) + ". Resolve them so both sides'"
                   " intent is kept; AI Flow commits the merge with your work.\n")
    prompt = tpl.format(key=t["key"], project=t["project"], summary=t["summary"], repo=c.project["repo"], branch=branch,
                      base=c.project["base"], checks=checks, description=t["description"] or "(no description)",
                      rework=rework, related=related_block(c.related), machine=machine_block(c.machine_tools),
                      environment=environment_notes(
                          c.project.get("environment"), "dev",
                          project_env(t["project"], c.project.get("environment"), "dev") if env is None else env),
                      test_time_slice=FULL_SUITE_TIME_SLICE_MINUTES, timeout_retry=retry_context)
    return with_knowledge_context(prompt, c.context_pack)


def fetch_ci_log(api: ControlClient, c: Claimed, branch: str, lines: int = 120) -> str:
    """Tail of the failing CI job log of this branch's pull request, through the control API ("" when there is none)."""
    if not (c.branch or c.findings):
        return ""  # a first run has no pull request yet
    try:
        r = api.call("GET", "/v1/github/ci-log", params={"run_id": c.run_id, "fence": c.fence, "branch": branch,
                                                          "lines": lines})
    except Conflict:
        raise
    except Exception as e:  # noqa: BLE001 - the log is a help, never a reason to stop the run
        log.warning("CI log of %s unavailable: %s", c.ticket["key"], e)
        return ""
    if not r or not r.get("found"):
        return ""
    steps = f" (failed step: {', '.join(r['failed_steps'])})" if r.get("failed_steps") else ""
    return f"Job: {r.get('job', '')}{steps} {r.get('url', '')}\n```\n{r.get('tail', '')}\n```"


def classify_question(api: ControlClient, c: Claimed, question: str) -> dict | None:
    """Ask the control plane to classify the question the AI stopped on (docs/v4/escalations.md); None when it
    cannot, so the old path (Needs owner and triage) still works."""
    try:
        return api.call("POST", "/v1/escalations/raise", {"run_id": c.run_id, "fence": c.fence, "question": question})
    except Conflict:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("classification of the question of %s failed: %s", c.ticket["key"], e)
        return None


def with_knowledge_context(prompt: str, pack: dict | None) -> str:
    """Append the stored, project-isolated context pack to any AI worker prompt."""
    if not pack or not pack.get("items"):
        return prompt
    labels = {"study_case": "Study case", "decision": "Decision", "convention": "Convention",
              "skill": "Skill", "guide": "Guide"}
    lines = ["\nKnowledge context pack (stored AI Flow knowledge; check the ticket if details conflict):"]
    for item in pack["items"]:
        scope = "general" if item.get("scope") == "general" else item.get("project", "")
        if item.get("epic"):
            scope += f" / epic {item['epic']}"
        stale = " [flagged for review]" if item.get("status") == "stale" else ""
        lines.append(f"- [{labels.get(item.get('kind'), item.get('kind'))}; {scope}{stale}] {item.get('title', '')}: "
                     f"{item.get('content', '')}")
    return prompt + "\n".join(lines) + "\n"


def machine_block(tools: dict) -> str:
    """'Tools on this machine' section of the prompt, so the AI does not try what is not installed."""
    if not tools:
        return ""
    available = ", ".join(f"{n} ({v})" if v else n for n, v in (tools.get("available") or {}).items())
    lines = ["\nTools on this machine (reported by its node agent just now):",
             f"- available: {available or 'none reported'}"]
    if tools.get("services"):
        lines.append(f"- test services running: {', '.join(tools['services'])}")
    if tools.get("missing"):
        lines.append(f"- not available: {', '.join(tools['missing'])}")
    lines.append("Do not try a tool that is not available. If a check of the ticket needs one, stop and report\n"
                 "`RESULT: BLOCKED: MISSING-TOOL: <tool>[, <tool>] - <what could not run>`; AI Flow then waits for a machine\n"
                 "that has it. Never ask the owner to install or fix a tool.")
    return "\n".join(lines) + "\n"


RELATED_KIND = {"ticket": "ticket", "run_summary": "run summary", "finding": "verify finding", "answer": "answer"}


def related_block(hits: list[dict]) -> str:
    """'Related past work' section of the prompt from the claim's memory hits; empty when there are none."""
    if not hits:
        return ""
    lines = [f"- {h.get('ticket') or h.get('ref')} ({RELATED_KIND.get(h.get('kind', ''), h.get('kind', ''))}):"
             f" {h.get('snippet', '')}" for h in hits]
    return ("\nRelated past work (from AI Flow memory; advice only, the ticket wins on any difference):\n"
            + "\n".join(lines) + "\n")


def pr_title(ticket: dict) -> str:
    """Conventional title with the ticket key; ASCII subjects keep it at the end, others start with the key."""
    title = ticket["summary"].strip().rstrip(".")
    title = title[0].upper() + title[1:] if title else ticket["key"]
    kind = {"Bug": "fix", "Hotfix": "fix", "Chore": "chore", "Docs": "docs", "Tech Debt": "refactor"}.get(ticket.get("type", ""), "feat")
    if not re.match(r"^[A-Z]", title):
        prefix = f"{kind}: {ticket['key']}: "
        return prefix + title[:100 - len(prefix)]
    suffix = f" ({ticket['key']})"
    return f"{kind}: {title}"[:100 - len(suffix)] + suffix


def commit_message(c: Claimed, summary: str) -> str:
    head = pr_title(c.ticket)
    return (f"{head}\n\n{summary}\n\nRefs: {c.ticket['key']}\nAI-Run: {c.run_id} ({c.account}, {c.provider}"
            f"{'/' + c.model if c.model else ''})\n")


def _suite_status(text: str, timed_out: bool) -> dict[str, str]:
    match = FULL_SUITE.search(text or "")
    if timed_out:
        return {"status": "unfinished", "reason": (match.group(2).strip() if match and match.group(2)
                                                        else "developer run reached its hard time limit")}
    if match:
        return {"status": match.group(1).lower(), "reason": (match.group(2) or "").strip()}
    return {"status": "unreported", "reason": "the developer response did not report full-suite completion"}


def _handover_timed_out(api: ControlClient, c: Claimed, git: Git, worktree: Path, branch: str,
                        repo: str, summary: str, handed: dict, result: dict, started: float) -> bool:
    """Commit available work and hand an interrupted full suite to PR checks."""
    changed = git.changed(worktree)
    if not changed:
        return False
    final_summary = (summary or "Developer run reached its hard time limit")[:500]
    sha = git.commit_all(worktree, commit_message(c, final_summary), c.identity)
    credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                          "repo": repo, "branch": branch})
    git.push(worktree, branch, credential["token"])
    note_text = (f"The full suite did not finish before this run's hard limit. CI checks must finish the suite; "
                 f"the available changes were handed over as {c.ticket['key']} on {branch}.")
    body = (f"Jira: {c.ticket['key']}\n\n## Summary\n{note_text}\n\n## Tests\n"
            "The full suite was not finished before the developer run limit. The pull request pipeline must complete "
            "the configured checks before merge.\n\n" + handover.pr_sections(handed)
            + f"## Run\n- account: {c.account} ({c.provider}{'/' + c.model if c.model else ''})\n"
            f"- run: {c.run_id}, fence {c.fence}\n- files changed: {len(changed)}\n")
    pr = api.call("POST", "/v1/github/pr", {"run_id": c.run_id, "fence": c.fence, "repo": repo,
                                             "head": branch, "base": c.project["base"],
                                             "title": pr_title(c.ticket), "body": body, "draft": True})
    jira = lambda path, **payload: api.call("POST", f"/v1/jira/{c.ticket['key']}/{path}",
                                             {"run_id": c.run_id, "fence": c.fence, **payload})
    jira("comment", body=note(c, "full suite handed to pipeline",
                              what="The full suite did not finish before the developer run limit",
                              why="The run preserved its available change and opened a draft pull request",
                              result=f"{note_text} {pr['url']}",
                              next_="The pull request pipeline completes the configured checks before merge",
                              t0=started, node=api_node(api)))
    jira("transition", to="In Verify")
    result.update(outcome="done", error_kind="timeout", pr_url=pr["url"], head_sha=sha,
                  summary=note_text[:1000])
    result.setdefault("details", {}).update(full_suite={"status": "unfinished",
                                                         "reason": "developer run reached its hard time limit"},
                                             handed_to_pipeline=True)
    return True


@dataclass
class DevSession:
    api: ControlClient
    claimed: Claimed
    git: Git
    heartbeat: Heartbeat
    started_at: float
    result: dict
    key: str
    project: dict
    repo: str
    branch: str
    worktree: Path | None = None
    branch_commit_count: int = 0
    conflicts: list[str] = field(default_factory=list)
    response: object | None = None
    summary: str = ""
    tests: str = ""
    handover: dict = field(default_factory=dict)
    suite_status: dict = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)


def _jira(session: DevSession, path: str, **body) -> dict:
    return session.api.call("POST", f"/v1/jira/{session.key}/{path}",
                            {"run_id": session.claimed.run_id, "fence": session.claimed.fence, **body})


def _prepare_worktree(session: DevSession, srv_root: str) -> None:
    c, git, project, branch, api = session.claimed, session.git, session.project, session.branch, session.api
    credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                          "repo": session.repo})
    mirror = git.mirror(session.repo, credential["token"], credential.get("transport_url", ""))
    session.worktree = Path(srv_root) / "work" / project["key"] / branch
    moved = git.foreign_worktree(mirror, session.worktree, branch)
    if moved:
        log.warning("%s: repository changed from %s to %s; replacing worktree %s",
                    session.key, moved["old_repo"], session.repo, session.worktree)
        work = (f"{len(moved['commits'])} committed change(s) on branch {branch} in {moved['old_repo']}"
                + (" (the branch is also pushed there):\n" if moved["pushed"] else " (not pushed anywhere):\n")
                + "\n".join(f"- {item}" for item in moved["commits"][:20])) if moved["commits"] else "no committed work on the old branch"
        _jira(session, "comment", body=note(c, "repository changed",
              what=f"The ticket's repository changed from {moved['old_repo']} to {session.repo}",
              why="The old worktree belongs to the old repository, so the AI could not find the code in it",
              result=f"Old worktree and branch {branch} removed; {work}",
              next_=f"A new worktree is created from {session.repo} {project['base']}", node=api_node(api)))
        git.drop_foreign_worktree(session.worktree, branch, Path(moved["old_mirror"]))
    start = git.worktree(mirror, session.worktree, branch, project["base"])
    session.branch_commit_count = (git.commits_ahead(session.worktree, project["base"])
                                   if start == f"origin/{branch}" else 0)
    if start != f"origin/{project['base']}":
        session.conflicts = git.merge_base_in(session.worktree, project["base"], c.identity)
    session.heartbeat.progress = {"phase": "worktree", "start": start, "conflicts": len(session.conflicts)}
    if moved:
        session.heartbeat.progress["repo_change"] = f"{moved['old_repo']} -> {session.repo}"
    env = project_env(project["key"], project.get("environment"), "dev")
    _jira(session, "transition", to="In Progress")
    _jira(session, "fields", fields={"Workspace": environment_status(project.get("environment"), "dev", env),
                                      "Agent (AI)": {"value": PROVIDER_NAME.get(c.provider, c.provider)}})
    why = "Rework after verify findings" if c.findings else "The ticket is ready and its blockers are done"
    result = f", with the latest {project['base']} merged in ({len(session.conflicts)} conflicted files to resolve)" if session.conflicts else ""
    _jira(session, "comment", body=note(c, "started", what=f"Develop the ticket on branch {branch} from {start}{result}",
          why=why, next_="The AI implements the acceptance criteria and runs the checks; AI Flow commits and opens a draft PR",
          node=api_node(api)))


def _execute_dev_ai(session: DevSession, srv_root: str, control_url: str, run_as: list[str]) -> None:
    c, project, api, hb = session.claimed, session.project, session.api, session.heartbeat
    ci_log = fetch_ci_log(api, c, session.branch)
    logs = Path(srv_root) / "logs" / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    env = project_env(project["key"], project.get("environment"), "dev")
    spec = RunSpec(prompt=build_prompt(c, session.branch, session.conflicts, ci_log, env), cwd=str(session.worktree),
                   account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model, effort=c.effort,
                   timeout_s=int(project.get("work_minutes", {}).get("hard", 60)) * 60,
                   control_url=control_url, run_token=c.run_token,
                   log_path=str(logs / f"{session.key}-{c.run_id}.jsonl"), extra_env=env)
    adapter = get_adapter(c.provider)
    base_argv = adapter.argv
    adapter.argv = lambda current: run_as + base_argv(current)
    wordpress = declares_wordpress(project.get("stacks") or project.get("stack"))
    runtime = _dev_runtime(spec, run_as, project, wordpress)
    with runtime as containers:
        if wordpress:
            hb.progress = {"phase": "wordpress-site"}
            start_wordpress_site(containers, session.worktree, project, c.run_id, spec)
            _jira(session, "comment", body=note(
                c, "WordPress test site ready", what="A temporary WordPress test site is ready",
                result="WP_BASE_URL (provided), WP_ADMIN_USER (provided), and WP_ADMIN_PASSWORD (provided) are set in the run environment",
                next_="Use `npx playwright test` with the project config; Chromium, Firefox, and WebKit are installed",
                node=api_node(api)))
        session.response = adapter.run(spec, _dev_progress(session), stop=hb.lost)
    response = session.response
    session.result.update(tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=response.cost_usd,
                          quota=response.quota, error_kind=response.error_kind,
                          reset_at=response.reset_at.isoformat() if response.reset_at else None,
                          details={"verdict": response.verdict, "permission_denials": response.permission_denials,
                                   "verdict_reason": (response.verdict_reason or "")[:2000],
                                   "exit_code": response.exit_code, "duration_s": int(response.duration_s),
                                   "log": response.log_path})
    if hb.lost.is_set():
        raise LeaseLost()


def _dev_runtime(spec: RunSpec, run_as: list[str], project: dict, wordpress: bool):
    if project.get("containers") or wordpress:
        from .containers import ContainerRuntime
        return ContainerRuntime(spec, run_as)
    return nullcontext()


def _dev_progress(session: DevSession):
    events = {"n": 0}

    def on_progress(event: dict) -> None:
        events["n"] += 1
        session.heartbeat.progress = {"phase": "ai", "events": events["n"],
                                      "last": str(event.get("type", ""))[:40],
                                      "elapsed_s": int(time.time() - session.started_at)}
    return on_progress


def _record_dev_result(session: DevSession) -> None:
    response = session.response
    summary = SUMMARY.search(response.final_text or "")
    tests = TESTS.search(response.final_text or "")
    session.summary = summary.group(1).strip() if summary else ((response.final_text or "").strip()[-600:] or response.error_message)
    session.tests = tests.group(1).strip() if tests else ""
    session.result["summary"] = session.summary
    session.handover = handover.parse(response.final_text or "", session.claimed.ticket.get("description") or "")
    session.suite_status = _suite_status(response.final_text or "", response.error_kind == "timeout")
    session.result.setdefault("details", {}).update(session.handover, full_suite=session.suite_status)
    if session.suite_status["status"] == "unfinished":
        suffix = "full suite unfinished: " + session.suite_status["reason"]
        session.tests = (session.tests + "; " if session.tests else "") + suffix
        session.result["details"]["handed_to_pipeline"] = True


def _stop_for_quota(session: DevSession) -> None:
    response = session.response
    session.result["outcome"] = "quota"
    _jira(session, "comment", body=note(session.claimed, "paused", what="The run stopped before finishing",
          why=f"{session.claimed.account} cannot work now ({response.error_kind}): {response.error_message[:200]}",
          result="No change was pushed", next_="Back to To Do for another account; this account pauses until its limit resets",
          t0=session.started_at, node=api_node(session.api)))
    _jira(session, "transition", to="To Do")


def _environment_stop(session: DevSession, absent: list[str]) -> None:
    if not absent:
        reason = ("unaccepted missing environment report: name an item of the project's environment list, "
                  f"not free text ({session.response.verdict_reason})")[:1000]
        return _retry_failure(session, reason, "check could not run")
    session.result["outcome"] = "environment_missing"
    session.result["details"]["missing_environment"] = absent
    _jira(session, "comment", body=note(session.claimed, "waiting for an environment item",
          what="The run needs an item of the project environment that is not provided",
          why=f"Missing environment item(s): {', '.join(absent)}",
          result="No question goes to the owner; the item is recorded by name",
          next_="Back to To Do; AI Flow provides the item or raises one environment ticket",
          t0=session.started_at, node=api_node(session.api)))
    _jira(session, "transition", to="To Do")


def _missing_tools_stop(session: DevSession, missing: list[str], command_missing: list[str]) -> None:
    response, c = session.response, session.claimed
    raised = session.api.call("POST", "/v1/escalations/raise", {
        "run_id": c.run_id, "fence": c.fence,
        "question": (f"MISSING-TOOL: {', '.join(command_missing)} - executed command returned command not found"
                     if command_missing and not has_missing_marker(response.verdict_reason or "")
                     else response.verdict_reason)})
    if raised.get("resolved") and raised.get("outcome") == "auto_answered":
        session.result["outcome"] = "auto_answered"
        session.result["details"].update(escalation_class=raised.get("class", ""), missing_tools=[])
        cited = f" {redact(raised['citation'])}." if raised.get("citation") else ""
        _jira(session, "comment", body=note(c, "question answered without the owner",
              what="The developer AI stopped with a question that AI Flow answered automatically",
              why=redact(f"Classified as: {raised.get('label', 'Resolved automatically')} "
                         f"({raised.get('reason', 'the register answered this')}).{cited}"),
              result=redact(raised.get("answer", "")), next_="Back to To Do; the next run gets this answer in its prompt",
              t0=session.started_at, node=api_node(session.api)))
        _jira(session, "transition", to="To Do")
        return
    session.result["outcome"] = "tool_missing"
    session.result["details"].update(missing_tools=missing,
        tool_missing_evidence="explicit_marker" if parse_missing_requirements(response.verdict_reason or "")
        else "command_not_found", escalation_class=raised.get("class", ""))
    if command_missing:
        session.result["details"]["command_not_found_tools"] = command_missing
    _jira(session, "comment", body=note(c, "waiting for a check tool", what="A configured check could not run",
          why=f"Missing tool(s): {', '.join(missing)}. {redact(response.verdict_reason)[:300]}",
          result="No question goes to the owner; the missing tool is recorded by name",
          next_="Back to To Do; AI Flow offers it again when a worker with the tool is available",
          t0=session.started_at, node=api_node(session.api)))
    _jira(session, "transition", to="To Do")


def _question_stop(session: DevSession) -> None:
    response, c = session.response, session.claimed
    raised = classify_question(session.api, c, response.verdict_reason)
    if raised and raised.get("resolved"):
        session.result["outcome"] = raised["outcome"]
        if raised.get("missing_tools"):
            session.result["details"]["missing_tools"] = raised["missing_tools"]
        session.result["details"]["escalation_class"] = raised["class"]
        cited = f" {redact(raised['citation'])}." if raised.get("citation") else ""
        _jira(session, "comment", body=note(c, "question answered without the owner",
              what="The developer AI stopped with a question that AI Flow answered automatically",
              why=redact(f"Classified as: {raised['label']} ({raised['reason']}).{cited}"),
              result=redact(raised["answer"]), next_="Back to To Do; the next run gets this answer in its prompt",
              t0=session.started_at, node=api_node(session.api)))
        _jira(session, "transition", to="To Do")
        return
    label = f" [{raised['label']}]" if raised and raised.get("label") else ""
    session.result["outcome"] = "blocked"
    _jira(session, "comment", body=note(c, "question for the owner",
          what="The developer AI stopped before changing code", why=response.verdict_reason + label,
          next_="Answer in a comment and move the ticket back to To Do", t0=session.started_at,
          node=api_node(session.api)))
    _jira(session, "transition", to="Needs owner")


def _blocked_result(session: DevSession) -> bool:
    response, project = session.response, session.project
    if response.error_kind in ("quota", "auth"):
        _stop_for_quota(session)
        return True
    if response.verdict != "BLOCKED":
        return False
    env_names, _other, env_marked = parse_missing_environment(response.verdict_reason,
                                                              items_of(project.get("environment")))
    if env_marked:
        _environment_stop(session, env_names)
        return True
    missing = parse_missing_requirements(response.verdict_reason)
    command_missing = parse_command_not_found_requirements(getattr(response, "command_results", []))
    missing = list(dict.fromkeys(missing + command_missing))
    machine = session.claimed.machine_tools or {}
    available = set(machine.get("available") or {}) | set(machine.get("services") or [])
    missing = [name for name in missing if name not in available]
    if has_missing_marker(response.verdict_reason) and not missing:
        _retry_failure(session, f"unaccepted missing requirement report: {response.verdict_reason}"[:1000],
                       "check could not run")
        return True
    if missing:
        _missing_tools_stop(session, missing, command_missing)
    else:
        _question_stop(session)
    return True


def _retry_failure(session: DevSession, reason: str, title: str) -> dict:
    session.result.update(outcome="failed", summary=reason[:1000])
    _jira(session, "comment", body=note(session.claimed, title,
          what="The run ended without a change AI Flow can push", why=reason[:600],
          result=(session.response.final_text or session.summary)[:600],
          next_="Back to To Do; the next run retries after a short wait",
          t0=session.started_at, node=api_node(session.api)))
    _jira(session, "transition", to="To Do")
    return session.result


def _reuse_existing_work(session: DevSession) -> dict:
    c, api, git = session.claimed, session.api, session.git
    branch = session.branch
    found = api.call("GET", "/v1/github/pr", params={"run_id": c.run_id, "fence": c.fence,
                                                      "repo": session.repo, "head": branch})
    existing = found if found and found.get("found") else None
    if not existing and not session.branch_commit_count:
        reason = (f"no changes to verify: branch {branch} has no committed work for this ticket and no open pull request")
        session.result.update(outcome="failed", error_kind="no_work", summary=reason[:1000])
        session.result.setdefault("details", {}).update(no_changes=True, no_work_reason=reason)
        _jira(session, "comment", body=note(c, "no work to verify",
              what="The developer found no changes and the ticket branch has no committed work", why=reason,
              result=session.summary[:600] if session.summary else "The developer reported no changes needed",
              next_="The flow doctor records the cause and decides whether to retry after the normal backoff",
              t0=session.started_at, node=api_node(api)))
        _jira(session, "transition", to="To Do")
        return session.result
    return _handover_existing_work(session, existing)


def _handover_existing_work(session: DevSession, existing: dict | None) -> dict:
    c, api, git = session.claimed, session.api, session.git
    pushed = False
    if session.branch_commit_count:
        credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                              "repo": session.repo, "branch": session.branch})
        git.push(session.worktree, session.branch, credential["token"])
        pushed = True
    sha = (git.run("rev-parse", "HEAD", cwd=session.worktree).strip() if pushed or not existing
           else existing.get("head_sha") or git.run("rev-parse", "HEAD", cwd=session.worktree).strip())
    pr = existing or _open_existing_work_pr(session)
    note_text = (f"The existing pull request already contains the ticket work: {pr['url']}" if existing else
                 f"The ticket branch already contains {session.branch_commit_count} committed change(s); "
                 f"AI Flow opened a draft pull request: {pr['url']}")
    session.result.setdefault("details", {}).update(already_done=True, no_changes=True,
                                                     branch_commits=session.branch_commit_count)
    session.result.update(outcome="done", summary=f"Already done: no new changes were needed; {note_text}."[:1000],
                          pr_url=pr["url"], head_sha=sha)
    _jira(session, "comment", body=note(session.claimed, "already done; sent to verification",
          what="The developer found the work already on the ticket branch", result=note_text,
          next_="Another account verifies the pull request against the acceptance criteria",
          t0=session.started_at, node=api_node(session.api)))
    _jira(session, "transition", to="In Verify")
    return session.result


def _open_existing_work_pr(session: DevSession) -> dict:
    c = session.claimed
    body = (f"Jira: {c.ticket['key']}\n\n## Summary\nThe existing ticket branch already contains the work; "
            "this developer run made no additional changes.\n\n"
            f"## Tests\n{session.tests or 'see the existing branch history'}\n\n"
            f"{handover.pr_sections(session.handover)}## Run\n"
            f"- account: {c.account} ({c.provider}{'/' + c.model if c.model else ''})\n"
            f"- run: {c.run_id}, fence {c.fence}\n- existing branch commits: {session.branch_commit_count}\n\n"
            "Opened as a draft by AI Flow v4; verification runs next (In Verify).")
    return session.api.call("POST", "/v1/github/pr", {"run_id": c.run_id, "fence": c.fence,
        "repo": session.repo, "head": session.branch, "base": session.project["base"],
        "title": pr_title(c.ticket), "body": body, "draft": True})


def _finish_changes(session: DevSession) -> dict:
    session.changed = session.git.changed(session.worktree)
    response = session.response
    if response.error_kind == "timeout" and _handover_timed_out(
            session.api, session.claimed, session.git, session.worktree, session.branch, session.repo,
            session.summary, session.handover, session.result, session.started_at):
        return session.result
    session.result.setdefault("details", {})["changed_files"] = session.changed[:100]
    conflicted = any(CONFLICT_MARKER.search((session.worktree / name).read_text(errors="ignore"))
                     for name in session.conflicts if (session.worktree / name).is_file())
    if conflicted:
        response.error_kind = response.error_kind or "unresolved conflicts"
    failed = response.error_kind or response.verdict == "FAIL" or not session.changed
    if failed:
        clean = not response.error_kind and response.verdict != "FAIL" and not session.changed
        if clean:
            return _reuse_existing_work(session)
        reason = response.error_kind or (response.verdict_reason or "AI reported RESULT: FAIL")
        return _retry_failure(session, f"{reason}: {session.summary}"[:1000], "no usable change")
    return _publish_changes(session)


def _publish_changes(session: DevSession) -> dict:
    c, api, git = session.claimed, session.api, session.git
    session.heartbeat.progress = {"phase": "commit", "files": len(session.changed)}
    sha = git.commit_all(session.worktree, commit_message(c, session.summary), c.identity)
    credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                          "repo": session.repo, "branch": session.branch})
    git.push(session.worktree, session.branch, credential["token"])
    suite_note = ("\n\nThe full suite did not finish in the developer run; the pull request pipeline must complete "
                  "the configured checks before merge." if session.suite_status["status"] == "unfinished" else "")
    body = (f"Jira: {session.key}\n\n## Summary\n{session.summary}\n\n## Tests\n"
            f"{session.tests or 'see the run log'}{suite_note}\n\n{handover.pr_sections(session.handover)}"
            f"## Run\n- account: {c.account} ({c.provider}{'/' + c.model if c.model else ''})\n"
            f"- run: {c.run_id}, fence {c.fence}\n- files changed: {len(session.changed)}\n\n"
            "Opened as a draft by AI Flow v4; verification runs next (In Verify).")
    pr = api.call("POST", "/v1/github/pr", {"run_id": c.run_id, "fence": c.fence, "repo": session.repo,
        "head": session.branch, "base": session.project["base"], "title": pr_title(c.ticket),
        "body": body, "draft": True})
    _jira(session, "comment", body=note(c, "draft PR opened", what=session.summary[:700],
          result=f"{len(session.changed)} files committed as {sha[:10]} on {session.branch}; draft PR {pr['url']}. Tests: {session.tests or 'not reported'}",
          next_=("The pull request pipeline completes the configured checks before merge"
                 if session.suite_status["status"] == "unfinished" else
                 "Another account verifies the PR against the acceptance criteria and merges it"),
          t0=session.started_at, node=api_node(api)))
    _jira(session, "transition", to="In Verify")
    session.result.update(outcome="done", pr_url=pr["url"], head_sha=sha)
    return session.result


def _finish_dev_response(session: DevSession) -> dict:
    _record_dev_result(session)
    if _blocked_result(session):
        return session.result
    return _finish_changes(session)


def run_dev(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
            run_as: list[str]) -> dict:
    project = c.project
    branch = c.branch or f"{c.ticket['key']}-{slug(c.ticket['summary'])}"
    heartbeat = Heartbeat(api, c, heartbeat_s)
    session = DevSession(api, c, Git(srv_root, run_as=run_as), heartbeat, time.time(),
                         {"fence": c.fence, "outcome": "failed", "branch": branch}, c.ticket["key"],
                         project, project["repo"], branch)
    heartbeat.start()
    try:
        _prepare_worktree(session, srv_root)
        _execute_dev_ai(session, srv_root, control_url, run_as)
        return _finish_dev_response(session)
    except LeaseLost:
        session.result.update(outcome="lost", summary="lease lost during the run; stopped without writing")
        return session.result
    except Conflict:
        session.result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return session.result
    except Exception as exc:  # noqa: BLE001
        log.exception("dev step failed")
        session.result.update(outcome="failed", summary=f"worker error: {exc}"[:1000])
        try:
            _jira(session, "comment", body=note(c, "worker error",
                  what="The worker failed before the AI could finish", why=str(exc)[:400],
                  next_="Back to To Do; the next run retries after a short wait",
                  t0=session.started_at, node=api_node(api)))
            _jira(session, "transition", to="To Do")
        except Exception:  # noqa: BLE001
            pass
        return session.result
    finally:
        heartbeat.stop.set()

def api_node(api: ControlClient) -> str:
    return getattr(api, "node_name", "node")
