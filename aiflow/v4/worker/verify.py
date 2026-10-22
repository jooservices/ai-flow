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
from pathlib import Path

from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import REPO_ROOT, Claimed, Heartbeat, LeaseLost, pr_title
from .dev import api_node, related_block
from .gitops import Git
from .notes import note, project_env

log = logging.getLogger("aiflow4.verify")
VERDICT = re.compile(r"^VERDICT:\s*(PASS|FAIL|BLOCKED)\b", re.M)
CHECKS = re.compile(r"^CHECKS:\s*(.+)$", re.M)


def findings_of(text: str) -> str:
    m = re.search(r"^FINDINGS:\s*\n((?:\s*[-*].*\n?)+)", text or "", re.M)
    return m.group(1).strip() if m else ""


def build_prompt(c: Claimed, pr: dict) -> str:
    tpl = (REPO_ROOT / "prompts" / "v4" / "verify.md").read_text(encoding="utf-8")
    checks = "\n".join(f"   - {x}" for x in c.project.get("checks") or []) or "   - the project's test suite"
    t = c.ticket
    return tpl.format(key=t["key"], project=t["project"], summary=t["summary"], base=c.project["base"],
                      pr_number=pr["number"], branch=pr["branch"], checks=checks,
                      dev_summary=pr.get("dev_summary") or "(none)", description=t["description"] or "(no description)",
                      related=related_block(c.related))


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
        credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                              "repo": project["repo"]})
        token = credential["token"]
        mirror = git.mirror(project["repo"], token, credential.get("transport_url", ""))
        if not git.remote_branch_exists(mirror, pr["branch"]):
            raise RuntimeError(f"branch {pr['branch']} is gone")
        wt = Path(srv_root) / "work" / project["key"] / f"{pr['branch']}--verify"
        head = git.detached(mirror, wt, f"origin/{pr['branch']}")
        conflicts = git.merge_base_in(wt, project["base"], c.identity, commit=False)
        hb.progress = {"phase": "verify", "head": head[:10]}
        if conflicts:
            git.abort_merge(wt)
            findings = "- merge: the branch conflicts with the latest " + project["base"] + " in " + ", ".join(conflicts[:12]) + \
                       ". Merge " + project["base"] + " into the branch and resolve the conflicts."
            return _rework(c, api, jira, pr_comment, result, findings, "conflicts with " + project["base"], t0=t0)
        jira("comment", body=note(c, "started", what=f"Verify PR #{pr['number']} at {head[:10]} merged with the latest {project['base']}",
                                  why=f"Developed by {pr.get('dev_account', 'another account')}; work is never graded by its author",
                                  next_="Check every acceptance criterion, run the checks, then merge or send findings back",
                                  node=api_node(api)))
        logs = Path(srv_root) / "logs" / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = RunSpec(prompt=build_prompt(c, pr), cwd=str(wt), account_home=str(Path(srv_root) / "accounts" / c.account),
                       model=c.model, effort=c.effort, timeout_s=int(project.get("work_minutes", {}).get("hard", 60)) * 60,
                       control_url=control_url, run_token=c.run_token, log_path=str(logs / f"{key}-{c.run_id}.jsonl"),
                       extra_env=project_env(project["key"]))
        adapter = get_adapter(c.provider)
        base_argv = adapter.argv
        adapter.argv = lambda s: run_as + base_argv(s)
        events = {"n": 0}

        def on_progress(ev: dict) -> None:
            events["n"] += 1
            hb.progress = {"phase": "verify-ai", "events": events["n"], "elapsed_s": int(time.time() - t0)}

        res = adapter.run(spec, on_progress, stop=hb.lost)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
                      error_kind=res.error_kind, reset_at=res.reset_at.isoformat() if res.reset_at else None,
                      head_sha=head)
        if hb.lost.is_set():
            raise LeaseLost()
        text = res.final_text or ""
        checks_m = CHECKS.search(text)
        checks = checks_m.group(1).strip() if checks_m else "not reported"
        verdict_m = VERDICT.search(text)
        verdict = verdict_m.group(1) if verdict_m else ""
        findings = findings_of(text)
        result["details"] = {"verdict": verdict, "checks": checks, "findings": findings, "exit_code": res.exit_code,
                             "duration_s": int(res.duration_s), "log": res.log_path,
                             "qc_passed": False, "ci": []}
        if res.error_kind in ("quota", "auth"):
            result.update(outcome="quota", summary=f"verifier {c.account} cannot work now ({res.error_kind}): {res.error_message}"[:500])
            return result
        if verdict == "BLOCKED":
            # the review itself could not run (sandbox, worktree): not the developer's fault, retry after backoff
            result.update(outcome="failed", error_kind="infra",
                          summary=f"review could not run: {(findings or text.strip())[-400:]}")
            return result
        if verdict == "FAIL":
            return _rework(c, api, jira, pr_comment, result, findings or text.strip()[-1500:], "verify failed", checks, t0=t0)
        if verdict != "PASS":
            result.update(outcome="failed", summary=f"no verdict ({res.error_kind or 'missing VERDICT line'}): "
                                                    f"{(res.error_message or text.strip())[-400:]}")
            return result
        # PASS from the reviewer; CI on the PR head must agree before anything is merged
        ci = wait_for_ci(api, c, pr.get("head_sha") or head, hb)
        failed = [x for x in ci if x["state"] == "failure"]
        result["details"].update(ci=ci, qc_passed=bool(ci) and all(x["state"] in ("success", "neutral") for x in ci))
        if failed:
            findings = "\n".join(f"- CI {x['name']}: failed ({x['url']})" for x in failed[:10])
            return _rework(c, api, jira, pr_comment, result, findings, "CI failed", checks, t0=t0)
        if ci:
            checks += " · CI: " + ", ".join(f"{x['name']} {x['state']}" for x in ci[:8])
        result["summary"] = f"verify passed; checks: {checks}"[:1000]
        needs_human = "require-human" in c.ticket.get("labels", []) or project.get("merge", {}).get("require_human")
        if needs_human or not project.get("merge", {}).get("auto", True):
            pr_comment(f"AI Flow verify: PASS by {c.account}.\n\nChecks: {checks}\n\nWaiting for a human review before merge.")
            jira("comment", body=note(c, "passed, waiting for a person", what=f"PR {pr['url']} meets the acceptance criteria",
                                      why="The ticket or project requires a human review before merge", result=f"Checks: {checks}",
                                      next_="Approve and merge the PR, or comment what to change", t0=t0, node=api_node(api)))
            jira("transition", to="Human review")
            result["outcome"] = "human"
            return result
        title = pr_title(c.ticket)
        m = merge_with_retries(api, c, pr, title, project.get("merge", {}).get("method", "squash"), hb)
        if not m.get("merged"):
            result.update(outcome="failed", summary=f"verify passed but the merge was refused: {m.get('message')}"[:500])
            jira("comment", body=note(c, "merge refused", what="Verify passed but GitHub refused the merge",
                                      why=str(m.get("message")), next_="The next verify run merges the latest base and tries again",
                                      t0=t0, node=api_node(api)))
            return result
        jira("comment", body=note(c, "merged", what=f"PR #{pr['number']} merged into {project['base']} as {m.get('sha', '')[:10]}",
                                  why="Every acceptance criterion is met and the checks pass", result=f"Checks: {checks}",
                                  next_="Ticket Done; tickets blocked by it can start", t0=t0, node=api_node(api)))
        jira("transition", to="Done")
        try:
            jira("fields", remove_labels=["rework"])
        except Exception:  # noqa: BLE001
            pass
        result.update(outcome="merged", head_sha=m.get("sha", head))
        return result
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
                       attempts: int = 6) -> dict:
    """Merge; when the base requires an up-to-date branch or checks are still expected, update the branch on
    GitHub, wait for CI on the new head and try again. A failing check stops the merge."""
    m: dict = {}
    for _ in range(attempts):
        m = api.call("POST", "/v1/github/merge", {"run_id": c.run_id, "fence": c.fence, "number": pr["number"],
                                                  "title": title, "method": method})
        if m.get("merged"):
            return m
        msg = (m.get("message") or "").lower()
        if m.get("mergeable_state") == "behind" or "not up to date" in msg or "behind" in msg:
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
