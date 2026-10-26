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
from contextlib import nullcontext
from pathlib import Path

from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import REPO_ROOT, Claimed, Heartbeat, LeaseLost, pr_title
from .dev import api_node, related_block, with_knowledge_context
from .gitops import Git
from .notes import environment_notes, note, project_env
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
                          project_env(t["project"], c.project.get("environment"), "verify") if env is None else env))
    return with_knowledge_context(prompt, c.context_pack)


def run_verify(api: ControlClient, c: Claimed, pr: dict, srv_root: str, control_url: str, heartbeat_s: int,
               run_as: list[str]) -> dict:
    key, project = c.ticket["key"], c.project
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    hb.start()
    t0 = time.time()
    result: dict = {"fence": c.fence, "outcome": "failed", "branch": pr["branch"], "pr_url": pr["url"]}

    def jira(path: str, **body) -> dict:
        return api.call("POST", f"/v1/jira/{key}/{path}", {"run_id": c.run_id, "fence": c.fence, **body})

    def pr_comment(text: str) -> None:
        try:
            api.call("POST", "/v1/github/comment", {"run_id": c.run_id, "fence": c.fence, "number": pr["number"], "body": text})
        except Conflict:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("PR comment failed: %s", e)

    try:
        setup, conflicts = _prepare_verify_workspace(api, c, pr, git, srv_root, hb, jira)
        if conflicts:
            findings = (f"- merge: the branch conflicts with the latest {project['base']} in "
                        f"{', '.join(conflicts[:12])}. Merge {project['base']} into the branch and resolve the conflicts.")
            result.setdefault("details", {})["reason"] = "conflict"
            return _rework(c, api, jira, pr_comment, result, findings,
                           "conflicts with " + project["base"], t0=t0)
        if setup["auto_updated"]:
            result["head_sha"] = setup["head"]
        res = _execute_review(api, c, pr, setup, srv_root, control_url, run_as, hb, jira, t0)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
                      error_kind=res.error_kind, reset_at=res.reset_at.isoformat() if res.reset_at else None,
                      head_sha=setup["head"])
        if hb.lost.is_set():
            raise LeaseLost()
        terminal = _handle_verdict(api, c, pr, res, result, t0, jira, pr_comment, setup)
        if terminal is not None:
            return terminal
        return _complete_pass(api, c, pr, setup, result, hb, jira, pr_comment, t0)
    except LeaseLost:
        result.update(outcome="lost", summary="lease lost during verify; stopped without writing")
        return result
    except Conflict:
        result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("verify step failed")
        result.update(outcome="failed", summary=f"verify worker error: {e}"[:1000])
        return result
    finally:
        hb.stop.set()
        try:  # leave the verify worktree clean for the next run (merge in progress, files the checks wrote)
            git.scrub(Path(srv_root) / "work" / project["key"] / f"{pr['branch']}--verify")
        except Exception:  # noqa: BLE001
            pass


def _prepare_verify_workspace(api: ControlClient, c: Claimed, pr: dict, git: Git, srv_root: str, hb: Heartbeat,
                              jira) -> tuple[dict, list[str]]:
    project = c.project
    credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                          "repo": project["repo"]})
    token = credential["token"]
    mirror = git.mirror(project["repo"], token, credential.get("transport_url", ""))
    if not git.remote_branch_exists(mirror, pr["branch"]):
        raise RuntimeError(f"branch {pr['branch']} is gone")
    worktree = Path(srv_root) / "work" / project["key"] / f"{pr['branch']}--verify"
    head = git.detached(mirror, worktree, f"origin/{pr['branch']}")
    conflicts = git.merge_base_in(worktree, project["base"], c.identity, commit=False)
    auto_updated = False
    if not conflicts and git.behind(worktree, project["base"]):
        git.abort_merge(worktree)
        auto_updated = bring_up_to_date(api, c, git, mirror, worktree, pr, project["base"], token,
                                        credential.get("transport_url", ""))
        if auto_updated:
            head = pr["head_sha"]
        conflicts = git.merge_base_in(worktree, project["base"], c.identity, commit=False)
    hb.progress = {"phase": "verify", "head": head[:10]}
    setup = {"worktree": worktree, "head": head, "auto_updated": auto_updated}
    if not conflicts:
        jira("comment", body=note(
            c, "started", what=f"Verify PR #{pr['number']} at {head[:10]} merged with the latest {project['base']}",
            why=f"Developed by {pr.get('dev_account', 'another account')}; work is never graded by its author",
            next_="Check every acceptance criterion, run the checks, then merge or send findings back",
            node=api_node(api)))
    else:
        git.abort_merge(worktree)
    return setup, conflicts


def _execute_review(api: ControlClient, c: Claimed, pr: dict, setup: dict, srv_root: str, control_url: str,
                    run_as: list[str], hb: Heartbeat, jira, t0: float):
    project = c.project
    worktree = setup["worktree"]
    logs = Path(srv_root) / "logs" / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    events = {"n": 0}

    def on_progress(_event: dict) -> None:
        events["n"] += 1
        hb.progress = {"phase": "verify-ai", "events": events["n"], "elapsed_s": int(time.time() - t0)}

    env = project_env(project["key"], project.get("environment"), "verify")
    spec = RunSpec(prompt=build_prompt(c, pr, env), cwd=str(worktree),
                   account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model,
                   effort=c.effort, timeout_s=int(project.get("work_minutes", {}).get("hard", 60)) * 60,
                   control_url=control_url, run_token=c.run_token,
                   log_path=str(logs / f"{c.ticket['key']}-{c.run_id}.jsonl"), extra_env=env)
    adapter = get_adapter(c.provider)
    base_argv = adapter.argv
    adapter.argv = lambda spec_: run_as + base_argv(spec_)
    wordpress = declares_wordpress(project.get("stacks") or project.get("stack"))
    if project.get("containers") or wordpress:
        from .containers import ContainerRuntime
        runtime = ContainerRuntime(spec, run_as)
    else:
        runtime = nullcontext()
    # The site is started through the container runtime, so its limits and crash cleanup own it.
    with runtime as containers:
        if wordpress:
            hb.progress = {"phase": "wordpress-site"}
            start_wordpress_site(containers, worktree, project, c.run_id, spec)
            jira("comment", body=note(
                c, "WordPress test site ready", what="A temporary WordPress test site is ready",
                result="WP_BASE_URL (provided), WP_ADMIN_USER (provided), and WP_ADMIN_PASSWORD (provided) are set in the run environment",
                next_="Use `npx playwright test` with the project config; Chromium, Firefox, and WebKit are installed",
                node=api_node(api)))
        return adapter.run(spec, on_progress, stop=hb.lost)


def _handle_verdict(api: ControlClient, c: Claimed, pr: dict, res, result: dict, t0: float, jira, pr_comment,
                    setup: dict) -> dict | None:
    text = res.final_text or ""
    checks_match = CHECKS.search(text)
    checks = checks_match.group(1).strip() if checks_match else "not reported"
    verdict_match = VERDICT.search(text)
    verdict = verdict_match.group(1) if verdict_match else ""
    findings = findings_of(text)
    result["details"] = {"verdict": verdict, "checks": checks, "findings": findings, "exit_code": res.exit_code,
                         "duration_s": int(res.duration_s), "log": res.log_path,
                         "qc_passed": False, "ci": [], "auto_updated": setup["auto_updated"],
                         "post_merge_criteria": deferred_criteria(text)}
    if res.error_kind in ("quota", "auth"):
        result.update(outcome="quota", summary=(f"verifier {c.account} cannot work now ({res.error_kind}): "
                                                 f"{res.error_message}")[:500])
        return result
    if verdict == "BLOCKED":
        result.update(outcome="failed", error_kind="infra",
                      summary=f"review could not run: {(findings or text.strip())[-400:]}")
        return result
    if verdict == "FAIL":
        return _rework(c, api, jira, pr_comment, result, findings or text.strip()[-1500:], "verify failed", checks, t0=t0)
    if verdict != "PASS":
        # Control retries at once on another account and bars this one after two in a row.
        result["details"]["no_verdict"] = True
        result.update(outcome="failed", summary=f"no verdict ({res.error_kind or 'missing VERDICT line'}): "
                                                    f"{(res.error_message or text.strip())[-400:]}")
        return result
    return None


def _complete_pass(api: ControlClient, c: Claimed, pr: dict, setup: dict, result: dict, hb: Heartbeat,
                   jira, pr_comment, t0: float) -> dict:
    checks = result["details"]["checks"]
    findings = _deferred_check_findings(result["details"]["post_merge_criteria"],
                                        set(c.project.get("checks") or []))
    if findings:
        return _rework(c, api, jira, pr_comment, result, findings,
                       "deferred criterion has no configured check", checks, t0=t0)
    checks, ci_findings = _complete_ci_checks(api, c, pr, setup, result, hb, checks)
    if ci_findings:
        return _rework(c, api, jira, pr_comment, result, ci_findings, "CI failed", checks, t0=t0)
    return _route_pass_completion(api, c, pr, setup, result, checks, hb, jira, pr_comment, t0)


def _deferred_check_findings(criteria: list[dict], configured_checks: set[str]) -> str:
    unmapped = [criterion for criterion in criteria
                if str(criterion.get("check") or "") not in configured_checks]
    if not unmapped:
        return ""
    ids = ", ".join(str(criterion.get("id") or "deferred criterion") for criterion in unmapped[:8])
    return (f"- post-merge verification: {ids} has no valid check from the project's configured checks. "
            "Map each deferred criterion to a configured check, or verify it before merge.")


def _complete_ci_checks(api: ControlClient, c: Claimed, pr: dict, setup: dict, result: dict, hb: Heartbeat,
                        checks: str) -> tuple[str, str]:
    ci = wait_for_ci(api, c, pr.get("head_sha") or setup["head"], hb)
    failed = [item for item in ci if item["state"] == "failure"]
    qc_passed = bool(ci) and all(item["state"] in ("success", "neutral") for item in ci)
    result["details"].update(ci=ci, qc_passed=qc_passed)
    if failed:
        findings = "\n".join(f"- CI {item['name']}: failed ({item['url']})" for item in failed[:10])
        return checks, findings
    if ci:
        checks += " · CI: " + ", ".join(f"{item['name']} {item['state']}" for item in ci[:8])
    return checks, ""


def _route_pass_completion(api: ControlClient, c: Claimed, pr: dict, setup: dict, result: dict, checks: str,
                           hb: Heartbeat, jira, pr_comment, t0: float) -> dict:
    project = c.project
    result["summary"] = f"verify passed; checks: {checks}"[:1000]
    needs_human = "require-human" in c.ticket.get("labels", []) or project.get("merge", {}).get("require_human")
    if needs_human or not project.get("merge", {}).get("auto", True):
        return _handoff_for_human(api, c, pr, result, checks, jira, pr_comment, t0)
    return _complete_merge(api, c, pr, setup, result, checks, hb, jira, pr_comment, t0)


def _handoff_for_human(api: ControlClient, c: Claimed, pr: dict, result: dict, checks: str, jira, pr_comment,
                       t0: float) -> dict:
    pr_comment(f"AI Flow verify: PASS by {c.account}.\n\nChecks: {checks}\n\nWaiting for a human review before merge.")
    jira("comment", body=note(c, "passed, waiting for a person", what=f"PR {pr['url']} meets the acceptance criteria",
                              why="The ticket or project requires a human review before merge", result=f"Checks: {checks}",
                              next_="Approve and merge the PR, or comment what to change", t0=t0, node=api_node(api)))
    jira("transition", to="Human review")
    result["outcome"] = "human"
    return result


def _complete_merge(api: ControlClient, c: Claimed, pr: dict, setup: dict, result: dict, checks: str,
                    hb: Heartbeat, jira, pr_comment, t0: float) -> dict:
    project = c.project
    title = pr_title(c.ticket)
    merged = merge_with_retries(api, c, pr, title, project.get("merge", {}).get("method", "squash"), hb)
    if merged.get("conflict"):
        findings = (f"- merge: the branch conflicts with the latest {project['base']} (another pull request merged "
                    f"first). Merge {project['base']} into the branch and resolve the conflicts.")
        result["details"]["reason"] = "conflict"
        return _rework(c, api, jira, pr_comment, result, findings,
                       "conflicts with " + project["base"], checks, t0=t0)
    if not merged.get("merged"):
        result.update(outcome="failed", summary=f"verify passed but the merge was refused: {merged.get('message')}"[:500])
        jira("comment", body=note(c, "merge refused", what="Verify passed but GitHub refused the merge",
                                  why=str(merged.get("message")),
                                  next_="The next verify run merges the latest base and tries again",
                                  t0=t0, node=api_node(api)))
        return result
    jira("comment", body=note(c, "merged", what=f"PR #{pr['number']} merged into {project['base']} as "
                              f"{merged.get('sha', '')[:10]}",
                              why="Every acceptance criterion is met and the checks pass", result=f"Checks: {checks}",
                              next_="Ticket Done; tickets blocked by it can start", t0=t0, node=api_node(api)))
    jira("transition", to="Done")
    try:
        jira("fields", remove_labels=["rework"])
    except Exception:  # noqa: BLE001
        pass
    result.update(outcome="merged", head_sha=merged.get("sha", setup["head"]))
    return result


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
