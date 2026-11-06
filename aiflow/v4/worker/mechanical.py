"""Catch mechanical verify returns before hand-over: refresh the base and run the pipeline's fast checks.

Verification used to send work back for a stale or conflicting branch and for documentation-rule failures that
the developer step can catch itself. Before a ticket leaves development, this module merges the latest base,
resolves identical-sided conflicts, and runs the same fast CI checks (including the documentation check).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import local_gate
from ..core.environment import redact
from . import gate_step
from .run_steps import api_node, note, pr_title, set_run_progress


def refresh_base(run: Any) -> dict | None:
    """Fetch and merge the project base; refuse hand-over when a real conflict remains."""
    claim, project, git, worktree = run.claim, run.project, run.git, run.worktree
    credential = run.api.call("POST", "/v1/github/token", {"run_id": claim.run_id, "fence": claim.fence,
                                                            "repo": run.repo})
    git.mirror(run.repo, credential["token"], credential.get("transport_url", ""))
    set_run_progress(run.heartbeat, "refresh-base")
    remaining = git.merge_base_resolving(worktree, project["base"], claim.identity, commit=True)
    if remaining:
        return report_conflict(run, remaining)
    if git.behind(worktree, project["base"]):
        return report_stale(run)
    return None


def report_conflict(run: Any, files: list[str]) -> dict:
    base = run.project["base"]
    run.git.abort_merge(run.worktree)
    named = ", ".join(files[:12])
    reason = f"conflicts with {base} in {named}"
    run.result.update(outcome="failed", error_kind="conflict", summary=reason[:1000])
    run.result.setdefault("details", {}).update(reason="conflict", conflict_files=files[:20])
    run.reporter.comment(body=note(
        run.claim, "hand-over refused: real conflict",
        what=f"Merging the latest {base} into {run.branch} left real conflicts",
        why=reason, result="Conflicting files: " + named,
        next_="Back to To Do; the next developer run resolves the named files",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.transition(to="To Do")
    return run.result


def report_stale(run: Any) -> dict:
    base = run.project["base"]
    reason = f"branch {run.branch} is still behind {base} after the refresh"
    run.result.update(outcome="failed", error_kind="stale_branch", summary=reason[:1000])
    run.result.setdefault("details", {})["reason"] = "stale_branch"
    run.reporter.comment(body=note(
        run.claim, "hand-over refused: stale branch",
        what=f"The branch is still behind {base} after merging it in",
        why=reason, next_="Back to To Do; the next run refreshes the branch before hand-over",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.transition(to="To Do")
    return run.result


def fast_plan(base: str) -> list:
    """The pipeline's fast checks: every local-gate step that needs no test service (includes docs-check)."""
    return [step for step in local_gate.steps(base) if not step.needs_env]


def run_fast_checks(run: Any) -> str | dict:
    """Run the fast CI checks, including the documentation check, and refuse hand-over when one fails."""
    set_run_progress(run.heartbeat, "fast-checks")
    ctx = local_gate.Context(run.worktree, run.project.get("base") or "develop",
                             pr_title(run.claim.ticket), tuple(run.run_as or ()))
    try:
        gate = local_gate.run(ctx, fast_plan(ctx.base), gate_step.gate_env(run.env))
        reason = gate_step.failure_text(gate) if gate.failed else ""
        error_kind = "gate_failed"
    except Exception as exc:  # noqa: BLE001 - a check that cannot run must not hand over
        gate, reason, error_kind = None, f"the fast checks could not run: {type(exc).__name__}: {exc}", "gate_error"
    if gate is None or gate.failed:
        return report_check_failure(run, reason, error_kind)
    return gate_step.pr_note(gate)


def report_check_failure(run: Any, reason: str, error_kind: str) -> dict:
    saved = gate_step.keep_failed_work(
        run.git, run.worktree, Path(run.srv_root) / "logs" / "runs" / f"{run.key}-{run.claim.run_id}")
    run.git.run("reset", "--soft", "HEAD~1", cwd=run.worktree, check=False)
    run.result.update(outcome="failed", error_kind=error_kind, summary=reason[:1000])
    run.reporter.comment(body=note(
        run.claim, "fast checks failed", what="The pipeline's fast checks fail on the local commit",
        why=redact(reason)[:900], next_=f"Back to To Do; nothing was pushed; the failed work is saved as {saved}",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.transition(to="To Do")
    return run.result


def checks_after_refresh(run: Any, local_gate_runner) -> str | dict:
    """After a successful base refresh, run the project's gate or the built-in fast checks for ai-flow."""
    setting = run.project.get("local_gate")
    if setting == "ai-flow":
        return run_fast_checks(run)
    if gate_step.enabled(run.project):
        return local_gate_runner(run)
    return ""
