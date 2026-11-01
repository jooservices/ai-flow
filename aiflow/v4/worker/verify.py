"""The verify step: a different account reviews the PR head merged with the latest base, runs the checks and
decides. PASS merges (or hands to a person when the ticket requires one); FAIL sends the ticket back to To Do
with findings that the next dev run receives.

Outcomes:
  merged   PR merged into the base, ticket Done
  human    verify passed, ticket in Human review (require-human label or project setting)
  rework   findings written to Jira and the PR, ticket back to To Do with the rework label
  quota    the verifier cannot work now; the ticket stays In Verify for another account
  failed   anything else (the ticket stays In Verify)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import REPO_ROOT, Claimed, Heartbeat, LeaseLost, pr_title
from .dev import related_block, run_with_inbox, with_knowledge_context
from .gitops import Git
from .notes import environment_notes, note, project_env
from .run_steps import (RunReporter, adapter_for_run, api_node, make_run_spec,
                        project_environment, run_reporter, runtime_for_run, start_run,
                        stop_heartbeat, worktree_path)
from .wordpress import declares_wordpress, start_site as start_wordpress_site

log = logging.getLogger("aiflow4.verify")
VERDICT = re.compile(r"^VERDICT:\s*(PASS|FAIL|BLOCKED)\b", re.M)
CHECKS = re.compile(r"^CHECKS:\s*(.+)$", re.M)
QUEUE_POLL_S = 15
POST_MERGE_CHECKS = re.compile(r"^POST-MERGE CHECKS:\s*\n((?:\s*[-*].*\n?)+)", re.M | re.I)


def deferred_criteria(text: str) -> list[dict[str, str]]:
    """Read deferred criteria and their explicitly selected configured check command."""
    match = POST_MERGE_CHECKS.search(text or "")
    out = []
    for line in (match.group(1).splitlines() if match else []):
        value = re.sub(r"^[ \t]*[-*][ \t]*", "", line).strip()
        if not value:
            continue
        mapped = re.match(r"^(AC\d+)\s*:\s*(.*?)\s*\|\s*check\s*:\s*`?(.+?)`?$", value, re.I)
        if mapped:
            out.append({"id": mapped.group(1).upper(), "text": mapped.group(2).strip()[:600],
                        "check": mapped.group(3).strip()[:500]})
        else:
            criterion = re.match(r"^(AC\d+)\s*:\s*(.*)$", value, re.I)
            out.append({"id": criterion.group(1).upper() if criterion else "",
                        "text": (criterion.group(2) if criterion else value)[:600], "check": ""})
    return list({(item["id"], item["text"]): item for item in out}.values())[:20]


def findings_of(text: str) -> str:
    m = re.search(r"^FINDINGS:\s*\n((?:\s*[-*].*\n?)+)", text or "", re.M)
    return m.group(1).strip() if m else ""


def build_prompt(c: Claimed, pr: dict, env: dict[str, str] | None = None) -> str:
    tpl = (REPO_ROOT / "prompts" / "v4" / "verify.md").read_text(encoding="utf-8")
    checks = "\n".join(f"   - {x}" for x in c.project.get("checks") or []) or "   - the project's test suite"
    if c.project.get("containers"):
        checks += "\n   - Docker commands go through the run's rootless, worktree-confined runtime"
    t = c.ticket
    prompt = tpl.format(key=t["key"], project=t["project"], summary=t["summary"], base=c.project["base"],
                      pr_number=pr["number"], branch=pr["branch"], checks=checks,
                      dev_summary=pr.get("dev_summary") or "(none)", description=t["description"] or "(no description)",
                      related=related_block(c.related),
                      environment=environment_notes(
                          c.project.get("environment"), "verify",
                          project_environment(t["project"], c.project, "verify", project_env) if env is None else env))
    return with_knowledge_context(prompt, c.context_pack)


@dataclass
class VerifyRun:
    api: ControlClient
    claim: Claimed
    pr: dict
    srv_root: str
    control_url: str
    run_as: list[str]
    git: Git
    heartbeat: Heartbeat
    started_at: float
    reporter: RunReporter
    result: dict


@dataclass
class VerifyWorkspace:
    mirror: Any
    worktree: Path
    head: str
    auto_updated: bool = False


@dataclass
class VerifyProgress:
    heartbeat: Heartbeat
    started_at: float
    events: int = 0

    def __call__(self, _event: dict) -> None:
        self.events += 1
        self.heartbeat.progress = {"phase": "verify-ai", "events": self.events,
                                   "elapsed_s": int(time.time() - self.started_at)}


@dataclass
class VerifyReview:
    response: Any
    text: str
    checks: str
    verdict: str
    findings: str
    post_merge: list[dict]


@dataclass
class CollectedVerify:
    checks: str


@dataclass(frozen=True)
class PRCommenter:
    api: ControlClient
    claim: Claimed
    pr: dict

    def __call__(self, text: str) -> None:
        try:
            self.api.call("POST", "/v1/github/comment", {
                "run_id": self.claim.run_id, "fence": self.claim.fence, "number": self.pr["number"],
                "body": text, "kind": "verify"})
        except Conflict:
            raise
        except Exception as error:  # noqa: BLE001
            log.warning("PR comment failed: %s", error)


def run_verify(api: ControlClient, c: Claimed, pr: dict, srv_root: str, control_url: str, heartbeat_s: int,
               run_as: list[str]) -> dict:
    started = start_run(api, c, srv_root, run_as, heartbeat_s, Git, Heartbeat)
    result = {"fence": c.fence, "outcome": "failed", "branch": pr["branch"], "pr_url": pr["url"]}
    state = VerifyRun(api, c, pr, srv_root, control_url, run_as, started.git, started.heartbeat,
                      started.started_at, run_reporter(api, c), result)
    try:
        workspace = prepare_verify(state)
        if isinstance(workspace, dict):
            return workspace
        review = execute_verify(state, workspace)
        if isinstance(review, dict):
            return review
        collected = collect_verification(state, workspace, review)
        if isinstance(collected, dict):
            return collected
        return publish_verification(state, workspace, collected)
    except LeaseLost:
        result.update(outcome="lost", summary="lease lost during verify; stopped without writing")
        return result
    except Conflict:
        result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return result
    except Exception as error:  # noqa: BLE001
        log.exception("verify step failed")
        result.update(outcome="failed", summary=f"verify worker error: {error}"[:1000])
        return result
    finally:
        stop_heartbeat(started.heartbeat)
        scrub_verify_worktree(state)


def prepare_verify(state: VerifyRun) -> VerifyWorkspace | dict:
    """Fetch the PR worktree, merge the latest base and record the verify start."""
    c, pr, project = state.claim, state.pr, state.claim.project
    credential = state.api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                               "repo": project["repo"]})
    mirror = state.git.mirror(project["repo"], credential["token"], credential.get("transport_url", ""))
    if not state.git.remote_branch_exists(mirror, pr["branch"]):
        raise RuntimeError(f"branch {pr['branch']} is gone")
    wt = worktree_path(state.srv_root, project["key"], pr["branch"], "--verify")
    head = state.git.detached(mirror, wt, f"origin/{pr['branch']}")
    conflicts = state.git.merge_base_in(wt, project["base"], c.identity, commit=False)
    auto_updated = False
    if not conflicts and state.git.behind(wt, project["base"]):
        state.git.abort_merge(wt)
        auto_updated = bring_up_to_date(state.api, c, state.git, mirror, wt, pr, project["base"], credential["token"],
                                         credential.get("transport_url", ""))
        if auto_updated:
            head = pr["head_sha"]
            state.result["head_sha"] = head
        conflicts = state.git.merge_base_in(wt, project["base"], c.identity, commit=False)
    state.heartbeat.progress = {"phase": "verify", "head": head[:10]}
    workspace = VerifyWorkspace(mirror, wt, head, auto_updated)
    if conflicts:
        state.git.abort_merge(wt)
        return report_verify_conflict(state, conflicts)
    report_verify_started(state, head)
    logs = Path(state.srv_root) / "logs" / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    return workspace


def report_verify_conflict(state: VerifyRun, conflicts: list[str]) -> dict:
    base = state.claim.project["base"]
    findings = ("- merge: the branch conflicts with the latest " + base + " in " + ", ".join(conflicts[:12]) +
                ". Merge " + base + " into the branch and resolve the conflicts.")
    state.result.setdefault("details", {})["reason"] = "conflict"
    return _rework(state.claim, state.api, state.reporter.jira, PRCommenter(state.api, state.claim, state.pr),
                   state.result, findings, "conflicts with " + base, t0=state.started_at)


def report_verify_started(state: VerifyRun, head: str) -> None:
    c, pr, project = state.claim, state.pr, state.claim.project
    state.reporter.jira("comment", body=note(
        c, "started", what=f"Verify PR #{pr['number']} at {head[:10]} merged with the latest {project['base']}",
        why=f"Developed by {pr.get('dev_account', 'another account')}; work is never graded by its author",
        next_="Check every acceptance criterion, run the checks, then merge or send findings back",
        node=api_node(state.api)))


def execute_verify(state: VerifyRun, workspace: VerifyWorkspace) -> VerifyReview | dict:
    """Build the verifier request, run it in its runtime and parse its response."""
    c, project, pr = state.claim, state.claim.project, state.pr
    env = project_environment(project["key"], project, "verify", project_env)
    log_path = Path(state.srv_root) / "logs" / "runs" / f"{c.ticket['key']}-{c.run_id}.jsonl"
    spec = make_run_spec(build_prompt(c, pr, env), workspace.worktree, state.srv_root, c, project,
                         state.control_url, log_path, env)
    adapter = adapter_for_run(c.provider, state.run_as, get_adapter)
    wordpress = declares_wordpress(project.get("stacks") or project.get("stack"))
    runtime = runtime_for_run(spec, state.run_as, project, wordpress)
    response = run_verify_ai(state, workspace, spec, adapter, runtime, wordpress)
    state.result.update(tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=response.cost_usd,
                        quota=response.quota,
                        quota_observed_at=(getattr(response, "quota_observed_at", None).isoformat()
                                           if getattr(response, "quota_observed_at", None) else None),
                        error_kind=response.error_kind, reset_at=response.reset_at.isoformat() if response.reset_at else None,
                        head_sha=workspace.head)
    if state.heartbeat.lost.is_set():
        raise LeaseLost()
    review = parse_verify_review(response)
    state.result["details"] = {"verdict": review.verdict, "checks": review.checks, "findings": review.findings,
                               "exit_code": response.exit_code, "duration_s": int(response.duration_s),
                               "log": response.log_path, "qc_passed": False, "ci": [],
                               "auto_updated": workspace.auto_updated, "post_merge_criteria": review.post_merge}
    if response.error_kind in ("quota", "auth"):
        state.result.update(outcome="quota", summary=f"verifier {c.account} cannot work now ({response.error_kind}): {response.error_message}"[:500])
        return state.result
    if review.verdict == "BLOCKED":
        state.result.update(outcome="failed", error_kind="infra",
                            summary=f"review could not run: {(review.findings or review.text.strip())[-400:]}")
        return state.result
    return review


def run_verify_ai(state: VerifyRun, workspace: VerifyWorkspace, spec: RunSpec, adapter: Any,
                  runtime: Any, wordpress: bool) -> Any:
    with runtime as containers:
        if wordpress:
            state.heartbeat.progress = {"phase": "wordpress-site"}
            start_wordpress_site(containers, workspace.worktree, state.claim.project, state.claim.run_id, spec)
            report_wordpress_ready(state)
        return run_with_inbox(adapter, spec, state.heartbeat, VerifyProgress(state.heartbeat, state.started_at))


def report_wordpress_ready(state: VerifyRun) -> None:
    state.reporter.jira("comment", body=note(
        state.claim, "WordPress test site ready", what="A temporary WordPress test site is ready",
        result="WP_BASE_URL (provided), WP_ADMIN_USER (provided), and WP_ADMIN_PASSWORD (provided) are set in the run environment",
        next_="Use \x60npx playwright test\x60 with the project config; Chromium, Firefox, and WebKit are installed",
        node=api_node(state.api)))


def parse_verify_review(response: Any) -> VerifyReview:
    text = response.final_text or ""
    checks_match = CHECKS.search(text)
    verdict_match = VERDICT.search(text)
    return VerifyReview(response, text, checks_match.group(1).strip() if checks_match else "not reported",
                        verdict_match.group(1) if verdict_match else "", findings_of(text), deferred_criteria(text))


def collect_verification(state: VerifyRun, workspace: VerifyWorkspace,
                         review: VerifyReview) -> CollectedVerify | dict:
    """Apply verdict, deferred-check and CI rules before publishing the verification result."""
    project = state.claim.project
    configured_checks = set(project.get("checks") or [])
    if review.verdict == "FAIL":
        return rework_verify(state, review.findings or review.text.strip()[-1500:], "verify failed")
    if review.verdict != "PASS":
        state.result.update(outcome="failed", summary=f"no verdict ({review.response.error_kind or 'missing VERDICT line'}): "
                                                     f"{(review.response.error_message or review.text.strip())[-400:]}")
        return state.result
    unmapped = unmapped_deferred(review.post_merge, configured_checks)
    if unmapped:
        ids = deferred_ids(unmapped)
        findings = (f"- post-merge verification: {ids} has no valid check from the project's configured checks. "
                    "Map each deferred criterion to a configured check, or verify it before merge.")
        return rework_verify(state, findings, "deferred criterion has no configured check")
    return collect_ci(state, workspace, review)


def unmapped_deferred(criteria: list[dict], configured_checks: set[str]) -> list[dict]:
    return [criterion for criterion in criteria
            if str(criterion.get("check") or "") not in configured_checks]


def deferred_ids(criteria: list[dict]) -> str:
    return ", ".join(str(criterion.get("id") or "deferred criterion") for criterion in criteria[:8])


def rework_verify(state: VerifyRun, findings: str, why: str, checks: str = "") -> dict:
    return _rework(state.claim, state.api, state.reporter.jira, PRCommenter(state.api, state.claim, state.pr),
                   state.result, findings, why, checks, state.started_at)


def collect_ci(state: VerifyRun, workspace: VerifyWorkspace, review: VerifyReview) -> CollectedVerify | dict:
    ci = wait_for_ci(state.api, state.claim, state.pr.get("head_sha") or workspace.head, state.heartbeat)
    failed = [item for item in ci if item["state"] == "failure"]
    state.result["details"].update(ci=ci, qc_passed=bool(ci) and all(
        item["state"] in ("success", "neutral") for item in ci))
    if failed:
        findings = "\n".join(f"- CI {item['name']}: failed ({item['url']})" for item in failed[:10])
        return rework_verify(state, findings, "CI failed", review.checks)
    checks = review.checks
    if ci:
        checks += " · CI: " + ", ".join(f"{item['name']} {item['state']}" for item in ci[:8])
    state.result["summary"] = f"verify passed; checks: {checks}"[:1000]
    return CollectedVerify(checks)


def publish_verification(state: VerifyRun, workspace: VerifyWorkspace, collected: CollectedVerify) -> dict:
    """Send a passing review to a person or through the merge queue."""
    c, project, pr, checks = state.claim, state.claim.project, state.pr, collected.checks
    needs_human = "require-human" in c.ticket.get("labels", []) or project.get("merge", {}).get("require_human")
    if needs_human or not project.get("merge", {}).get("auto", True):
        return publish_human_review(state, checks)
    merged = merge_with_retries(state.api, c, pr, pr_title(c.ticket),
                                project.get("merge", {}).get("method", "squash"), state.heartbeat)
    if merged.get("conflict"):
        return report_merge_conflict_after_ci(state, checks)
    if not merged.get("merged"):
        return report_merge_refused(state, merged)
    return report_merged(state, merged, checks, workspace.head)


def publish_human_review(state: VerifyRun, checks: str) -> dict:
    c, pr = state.claim, state.pr
    PRCommenter(state.api, c, pr)(f"AI Flow verify: PASS by {c.account}.\n\nChecks: {checks}\n\nWaiting for a human review before merge.")
    state.reporter.jira("comment", body=note(
        c, "passed, waiting for a person", what=f"PR {pr['url']} meets the acceptance criteria",
        why="The ticket or project requires a human review before merge", result=f"Checks: {checks}",
        next_="Approve and merge the PR, or comment what to change", t0=state.started_at,
        node=api_node(state.api)))
    state.reporter.jira("transition", to="Human review")
    state.result["outcome"] = "human"
    return state.result


def report_merge_conflict_after_ci(state: VerifyRun, checks: str) -> dict:
    base = state.claim.project["base"]
    findings = (f"- merge: the branch conflicts with the latest {base} (another pull request merged "
                f"first). Merge {base} into the branch and resolve the conflicts.")
    state.result["details"]["reason"] = "conflict"
    return _rework(state.claim, state.api, state.reporter.jira, PRCommenter(state.api, state.claim, state.pr),
                   state.result, findings, "conflicts with " + base, checks, state.started_at)


def report_merge_refused(state: VerifyRun, merged: dict) -> dict:
    state.result.update(outcome="failed", summary=f"verify passed but the merge was refused: {merged.get('message')}"[:500])
    state.reporter.jira("comment", body=note(
        state.claim, "merge refused", what="Verify passed but GitHub refused the merge",
        why=str(merged.get("message")), next_="The next verify run merges the latest base and tries again",
        t0=state.started_at, node=api_node(state.api)))
    return state.result


def report_merged(state: VerifyRun, merged: dict, checks: str, head: str) -> dict:
    c = state.claim
    project = c.project
    state.reporter.jira("comment", body=note(
        c, "merged", what=f"PR #{state.pr['number']} merged into {project['base']} as {merged.get('sha', '')[:10]}",
        why="Every acceptance criterion is met and the checks pass", result=f"Checks: {checks}",
        next_="Ticket Done; tickets blocked by it can start", t0=state.started_at, node=api_node(state.api)))
    state.reporter.jira("transition", to="Done")
    try:
        state.reporter.jira("fields", remove_labels=["rework"])
    except Exception:  # noqa: BLE001
        pass
    state.result.update(outcome="merged", head_sha=merged.get("sha", head))
    return state.result


def scrub_verify_worktree(state: VerifyRun) -> None:
    try:  # leave the verify worktree clean for the next run (merge in progress, files the checks wrote)
        state.git.scrub(worktree_path(state.srv_root, state.claim.project["key"], state.pr["branch"], "--verify"))
    except Exception:  # noqa: BLE001
        pass


def bring_up_to_date(api: ControlClient, c: Claimed, git: Git, mirror, wt: Path, pr: dict, base: str, token: str,
                     transport_url: str = "", polls: int = 6, wait_s: float = 5) -> bool:
    """Merge the base into the PR branch on GitHub (a clean merge only) and move the verify worktree to the new head.
    False when GitHub refused (a real conflict, or no permission) or the new head did not show up: the caller then
    judges the branch as it was."""
    res = api.call("POST", "/v1/github/update-branch", {"run_id": c.run_id, "fence": c.fence, "number": pr["number"],
                                                        "body": ""})
    if not res.get("ok"):
        return False
    ref = f"origin/{pr['branch']}"
    for _ in range(polls):
        git.mirror(c.project["repo"], token, transport_url)
        if not git.behind(mirror, base, ref):
            pr["head_sha"] = git.detached(mirror, wt, ref)
            return True
        time.sleep(wait_s)
    return False


def _rework(c, api, jira, pr_comment, result: dict, findings: str, why: str, checks: str = "", t0: float | None = None) -> dict:
    body = f"AI Flow verify: FAIL ({why}).\n\nFindings:\n{findings}" + (f"\n\nChecks: {checks}" if checks else "")
    pr_comment(body)
    jira("comment", body=note(c, "sent back for rework", what=f"Verify did not pass ({why})", result="Findings:\n" + findings
                              + (f"\nChecks: {checks}" if checks else ""),
                              next_="Back to To Do with the rework label; the next dev run continues the branch with these findings",
                              t0=t0, node=api_node(api)))
    jira("fields", add_labels=["rework"])
    jira("transition", to="To Do")
    result.update(outcome="rework", summary=f"{why}: {findings}"[:1500])
    result.setdefault("details", {})["findings"] = findings
    return result


def wait_for_ci(api: ControlClient, c: Claimed, sha: str, hb: Heartbeat, max_wait_s: int = 900) -> list[dict]:
    """Checks on the PR head; waits while any is pending (heartbeats keep the lease). [] when the repo has no CI."""
    t_end = time.time() + max_wait_s
    while True:
        try:
            ci = api.call("GET", "/v1/github/checks", params={"run_id": c.run_id, "fence": c.fence, "sha": sha})["checks"]
        except Conflict:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("CI status unavailable: %s", e)
            return []
        if not any(x["state"] == "pending" for x in ci) or time.time() > t_end or hb.lost.is_set():
            return ci
        hb.progress = {"phase": "waiting for CI", "pending": [x["name"] for x in ci if x["state"] == "pending"]}
        time.sleep(30)


def merge_with_retries(api: ControlClient, c: Claimed, pr: dict, title: str, method: str, hb: Heartbeat,
                       attempts: int = 6, queue_wait_s: int = 2700) -> dict:
    """Merge through the control merge queue: ready pull requests merge one after another, so this call waits for its
    turn (the lease heartbeat keeps running). On its turn the control API first brings the branch up to date with
    the base; this function then waits for CI on the new head and tries again. A failing check or a real conflict
    stops the merge."""
    m: dict = {}
    attempt, queue_until = 0, time.time() + queue_wait_s
    while attempt < attempts:
        m = api.call("POST", "/v1/github/merge", {"run_id": c.run_id, "fence": c.fence, "number": pr["number"],
                                                  "title": title, "method": method})
        if m.get("merged"):
            return m
        if m.get("queued"):  # another pull request merges first; waiting for the turn is not a failed attempt
            if time.time() > queue_until or hb.lost.is_set():
                return {"merged": False, "message": "gave up waiting in the merge queue: " + str(m.get("message", ""))}
            time.sleep(QUEUE_POLL_S)
            continue
        attempt += 1
        if m.get("conflict") or m.get("update_failed"):  # a real conflict, or an update that failed for another reason
            return m
        msg = (m.get("message") or "").lower()
        if m.get("updated"):  # the branch was brought up to date on this turn; CI runs again on the new head
            time.sleep(20)
        elif m.get("mergeable_state") == "behind" or "not up to date" in msg or "behind" in msg:
            api.call("POST", "/v1/github/update-branch", {"run_id": c.run_id, "fence": c.fence, "number": pr["number"],
                                                          "body": ""})
            time.sleep(20)
        elif "expected" in msg or "status check" in msg or m.get("mergeable_state") in ("blocked", "unstable", "unknown"):
            time.sleep(30)
        else:
            return m
        head = m.get("head_sha") or pr.get("head_sha", "")
        ci = wait_for_ci(api, c, head, hb) if head else []
        if any(x["state"] == "failure" for x in ci):
            failed = ", ".join(x["name"] for x in ci if x["state"] == "failure")
            return {"merged": False, "message": f"CI failed after updating the branch: {failed}"}
    return m
