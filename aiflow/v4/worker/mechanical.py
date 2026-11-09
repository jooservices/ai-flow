"""Refresh developer branches and recognize the documentation gate's actionable missing-page failures."""
from __future__ import annotations

from typing import Any

from .run_steps import api_node, note, set_run_progress


def refresh_base(run: Any) -> dict | None:
    """Merge the latest base before hand-over and return real conflicts to development with their paths."""
    claim, project = run.claim, run.project
    credential = run.api.call("POST", "/v1/github/token", {"run_id": claim.run_id, "fence": claim.fence,
                                                              "repo": run.repo})
    run.git.mirror(run.repo, credential["token"], credential.get("transport_url", ""))
    set_run_progress(run.heartbeat, "refresh-base")
    conflicts = run.git.merge_base_resolving(run.worktree, project["base"], claim.identity, commit=True)
    if conflicts:
        return report_conflict(run, conflicts)
    if run.git.behind(run.worktree, project["base"]):
        return report_stale_branch(run)
    return None


def preserve_branch_after_abort(run: Any) -> None:
    """Leave the developer's committed work on its branch after aborting a conflicting local merge."""
    run.git.abort_merge(run.worktree)
    credential = run.api.call("POST", "/v1/github/token", {"run_id": run.claim.run_id,
                                                              "fence": run.claim.fence, "repo": run.repo,
                                                              "branch": run.branch})
    run.git.push(run.worktree, run.branch, credential["token"], expected_head=getattr(run, "branch_lease", None))


def report_conflict(run: Any, files: list[str]) -> dict:
    """Return a real merge conflict to development and name each conflicting path."""
    base = run.project["base"]
    preserve_branch_after_abort(run)
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


def report_stale_branch(run: Any) -> dict:
    """Refuse hand-over when the base merge did not make the branch current."""
    preserve_branch_after_abort(run)
    base = run.project["base"]
    reason = f"branch {run.branch} is still behind {base} after the refresh"
    run.result.update(outcome="failed", error_kind="stale_branch", summary=reason[:1000])
    run.result.setdefault("details", {})["reason"] = "stale_branch"
    run.reporter.comment(body=note(
        run.claim, "hand-over refused: stale branch",
        what=f"The branch is still behind {base} after merging it in", why=reason,
        next_="Back to To Do; the next developer run refreshes the branch before hand-over",
        t0=run.started_at, node=api_node(run.api)))
    run.reporter.transition(to="To Do")
    return run.result


def documentation_gap(result: Any) -> bool:
    """True only when docs-check reports a missing changed documentation page or route entry."""
    failed = result.failed
    if not failed or failed.name != "docs-check":
        return False
    tail = failed.tail.lower()
    return "documentation check failed:" in tail and (
        "without a change in docs/" in tail or "without changed documentation naming" in tail)
