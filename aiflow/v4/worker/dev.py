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

from ..adapters import RunSpec, get_adapter
from ..core.text import slug
from ..core.toolchains import parse_missing_requirements
from .client import Conflict, ControlClient
from .gitops import Git
from .notes import note, project_env

log = logging.getLogger("aiflow4.dev")
REPO_ROOT = Path(__file__).resolve().parents[3]
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


def build_prompt(c: Claimed, branch: str, conflicts: list[str] | None = None, ci_log: str = "") -> str:
    tpl = (REPO_ROOT / "prompts" / "v4" / "dev.md").read_text(encoding="utf-8")
    checks = "\n".join(f"  - {x}" for x in c.project.get("checks") or []) or "  - the project's test suite"
    t = c.ticket
    rework = ""
    if c.findings:
        rework += ("\nThis is a rework: the branch already holds your earlier work and a reviewer sent it back. Fix every"
                   " finding below, keep what was right, and run the checks again.\nFindings:\n" + c.findings.strip() + "\n")
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
                      rework=rework, related=related_block(c.related), machine=machine_block(c.machine_tools))
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


def run_dev(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
            run_as: list[str]) -> dict:
    key, project = c.ticket["key"], c.project
    repo = project["repo"]
    branch = c.branch or f"{key}-{slug(c.ticket['summary'])}"
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    hb.start()
    t0 = time.time()
    result: dict = {"fence": c.fence, "outcome": "failed", "branch": branch}

    def jira(path: str, **body) -> dict:
        return api.call("POST", f"/v1/jira/{key}/{path}", {"run_id": c.run_id, "fence": c.fence, **body})

    try:
        credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence, "repo": repo})
        token = credential["token"]
        mirror = git.mirror(repo, token, credential.get("transport_url", ""))
        wt = Path(srv_root) / "work" / project["key"] / branch
        moved = git.foreign_worktree(mirror, wt, branch)
        if moved:  # the ticket's repository changed since the worktree was made: never reuse it
            log.warning("%s: repository changed from %s to %s; replacing worktree %s", key, moved["old_repo"], repo, wt)
            work = (f"{len(moved['commits'])} committed change(s) on branch {branch} in {moved['old_repo']}"
                    + (" (the branch is also pushed there):\n" if moved["pushed"] else " (not pushed anywhere):\n")
                    + "\n".join(f"- {x}" for x in moved["commits"][:20])) if moved["commits"] else "no committed work on the old branch"
            jira("comment", body=note(c, "repository changed", what=f"The ticket's repository changed from {moved['old_repo']} to {repo}",
                                      why="The old worktree belongs to the old repository, so the AI could not find the code in it",
                                      result=f"Old worktree and branch {branch} removed; {work}",
                                      next_=f"A new worktree is created from {repo} {project['base']}", node=api_node(api)))
            git.drop_foreign_worktree(wt, branch, Path(moved["old_mirror"]))
        start = git.worktree(mirror, wt, branch, project["base"])
        conflicts: list[str] = []
        if start != f"origin/{project['base']}":  # continuing the branch: bring in the latest base first
            conflicts = git.merge_base_in(wt, project["base"], c.identity)
        hb.progress = {"phase": "worktree", "start": start, "conflicts": len(conflicts)}
        if moved:
            hb.progress["repo_change"] = f"{moved['old_repo']} -> {repo}"
        jira("transition", to="In Progress")
        jira("fields", fields={"Workspace": f"{c.account}@{api_node(api)}",
                               "Agent (AI)": {"value": PROVIDER_NAME.get(c.provider, c.provider)}})
        jira("comment", body=note(c, "started", what=f"Develop the ticket on branch {branch} from {start}"
                                  + (f", with the latest {project['base']} merged in ({len(conflicts)} conflicted files to resolve)" if conflicts else ""),
                                  why=("Rework after verify findings" if c.findings else "The ticket is ready and its blockers are done"),
                                  next_="The AI implements the acceptance criteria and runs the checks; AI Flow commits and opens a draft PR",
                                  node=api_node(api)))
        ci_log = fetch_ci_log(api, c, branch)
        logs = Path(srv_root) / "logs" / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = RunSpec(prompt=build_prompt(c, branch, conflicts, ci_log), cwd=str(wt),
                       account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model, effort=c.effort,
                       timeout_s=int(project.get("work_minutes", {}).get("hard", 60)) * 60, control_url=control_url,
                       run_token=c.run_token, log_path=str(logs / f"{key}-{c.run_id}.jsonl"),
                       extra_env=project_env(project["key"]))
        adapter = get_adapter(c.provider)
        adapter_argv = adapter.argv
        adapter.argv = lambda s: run_as + adapter_argv(s)  # the AI runs as the unprivileged AI user

        events = {"n": 0}

        def on_progress(ev: dict) -> None:
            events["n"] += 1
            hb.progress = {"phase": "ai", "events": events["n"], "last": str(ev.get("type", ""))[:40],
                           "elapsed_s": int(time.time() - t0)}

        res = adapter.run(spec, on_progress, stop=hb.lost)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
                      error_kind=res.error_kind, reset_at=res.reset_at.isoformat() if res.reset_at else None,
                      details={"verdict": res.verdict, "permission_denials": res.permission_denials,
                               "exit_code": res.exit_code, "duration_s": int(res.duration_s), "log": res.log_path})
        if hb.lost.is_set():
            raise LeaseLost()
        summary_m, tests_m = SUMMARY.search(res.final_text or ""), TESTS.search(res.final_text or "")
        summary = summary_m.group(1).strip() if summary_m else ((res.final_text or "").strip()[-600:] or res.error_message)
        tests = tests_m.group(1).strip() if tests_m else ""
        result["summary"] = summary

        if res.error_kind in ("quota", "auth"):
            result["outcome"] = "quota"
            jira("comment", body=note(c, "paused", what="The run stopped before finishing",
                                      why=f"{c.account} cannot work now ({res.error_kind}): {res.error_message[:200]}",
                                      result="No change was pushed", next_="Back to To Do for another account; this account pauses until its limit resets",
                                      t0=t0, node=api_node(api)))
            jira("transition", to="To Do")
            return result
        missing = parse_missing_requirements(res.verdict_reason) if res.verdict == "BLOCKED" else []
        if missing:
            # Record the class through the same fenced path as every other developer stop. The run result below
            # still carries all missing requirements so the normal tool-wait path is preserved.
            raised = api.call("POST", "/v1/escalations/raise", {
                "run_id": c.run_id, "fence": c.fence, "question": res.verdict_reason})
            if raised.get("resolved") and raised.get("outcome") == "auto_answered":
                result["outcome"] = "auto_answered"
                result["details"]["escalation_class"] = raised.get("class", "")
                result["details"]["missing_tools"] = []
                cited = f" {raised['citation']}." if raised.get("citation") else ""
                jira("comment", body=note(c, "question answered without the owner",
                                          what=f"The developer AI stopped with a question: {res.verdict_reason[:400]}",
                                          why=f"Classified as: {raised.get('label', 'Resolved automatically')} "
                                              f"({raised.get('reason', 'the register answered this')}).{cited}",
                                          result=raised.get("answer", ""),
                                          next_="Back to To Do; the next run gets this answer in its prompt",
                                          t0=t0, node=api_node(api)))
                jira("transition", to="To Do")
                return result
            result["outcome"] = "tool_missing"
            result["details"]["missing_tools"] = missing
            result["details"]["escalation_class"] = raised.get("class", "")
            jira("comment", body=note(c, "waiting for a machine with the tool", what="A check cannot run on this machine",
                                      why=f"{', '.join(missing)} missing on {api_node(api)}: {res.verdict_reason[:300]}",
                                      result="No question goes to the owner; the machine and the tool are recorded",
                                      next_="Back to To Do; AI Flow offers it again when a machine with the tool is available",
                                      t0=t0, node=api_node(api)))
            jira("transition", to="To Do")
            return result
        if res.verdict == "BLOCKED":
            # every escalation is classified when it is raised; only a new business decision or an irreversible or
            # production action may reach a person (docs/v4/escalations.md)
            raised = classify_question(api, c, res.verdict_reason)
            if raised and raised.get("resolved"):
                result["outcome"] = raised["outcome"]
                if raised.get("missing_tools"):
                    result["details"]["missing_tools"] = raised["missing_tools"]
                result["details"]["escalation_class"] = raised["class"]
                cited = f" {raised['citation']}." if raised.get("citation") else ""
                jira("comment", body=note(c, "question answered without the owner",
                                          what=f"The developer AI stopped with a question: {res.verdict_reason[:400]}",
                                          why=f"Classified as: {raised['label']} ({raised['reason']}).{cited}",
                                          result=raised["answer"],
                                          next_="Back to To Do; the next run gets this answer in its prompt",
                                          t0=t0, node=api_node(api)))
                jira("transition", to="To Do")
                return result
            label = f" [{raised['label']}]" if raised and raised.get("label") else ""
            result["outcome"] = "blocked"
            jira("comment", body=note(c, "question for the owner", what="The developer AI stopped before changing code",
                                      why=res.verdict_reason + label,
                                      next_="Answer in a comment and move the ticket back to To Do",
                                      t0=t0, node=api_node(api)))
            jira("transition", to="Needs owner")
            return result
        changed = git.changed(wt)
        result.setdefault("details", {})["changed_files"] = changed[:100]
        if conflicts and any(CONFLICT_MARKER.search((wt / f).read_text(errors="ignore")) for f in conflicts if (wt / f).is_file()):
            res.error_kind = res.error_kind or "unresolved conflicts"
        if res.error_kind or not changed:
            reason = res.error_kind or ("no changes" if not changed else "")
            result["outcome"] = "failed"
            result["summary"] = f"{reason}: {summary}"[:1000]
            jira("comment", body=note(c, "no usable change", what="The run ended without a change AI Flow can push",
                                      why=reason, result=(summary[:600] if summary else "no output"),
                                      next_="Back to To Do; the next run retries after a short wait", t0=t0, node=api_node(api)))
            jira("transition", to="To Do")
            return result
        hb.progress = {"phase": "commit", "files": len(changed)}
        sha = git.commit_all(wt, commit_message(c, summary), c.identity)
        credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                              "repo": repo, "branch": branch})
        token = credential["token"]
        git.push(wt, branch, token)
        body = (f"Jira: {key}\n\n## Summary\n{summary}\n\n## Tests\n{tests or 'see the run log'}\n\n"
                f"## Run\n- account: {c.account} ({c.provider}{'/' + c.model if c.model else ''})\n"
                f"- run: {c.run_id}, fence {c.fence}\n- files changed: {len(changed)}\n\n"
                "Opened as a draft by AI Flow v4; verification runs next (In Verify).")
        pr = api.call("POST", "/v1/github/pr", {"run_id": c.run_id, "fence": c.fence, "repo": repo, "head": branch,
                                                "base": project["base"], "title": pr_title(c.ticket),
                                                "body": body, "draft": True})
        jira("comment", body=note(c, "draft PR opened", what=summary[:700],
                                  result=f"{len(changed)} files committed as {sha[:10]} on {branch}; draft PR {pr['url']}. Tests: {tests or 'not reported'}",
                                  next_="Another account verifies the PR against the acceptance criteria and merges it",
                                  t0=t0, node=api_node(api)))
        jira("transition", to="In Verify")
        result.update(outcome="done", pr_url=pr["url"], head_sha=sha)
        return result
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
            jira("comment", body=note(c, "worker error", what="The worker failed before the AI could finish",
                                      why=str(e)[:400], next_="Back to To Do; the next run retries after a short wait",
                                      t0=t0, node=api_node(api)))
            jira("transition", to="To Do")
        except Exception:  # noqa: BLE001
            pass
        return result
    finally:
        hb.stop.set()


def api_node(api: ControlClient) -> str:
    return getattr(api, "node_name", "node")
