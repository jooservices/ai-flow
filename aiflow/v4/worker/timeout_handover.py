"""Hand a timed-out developer run with work to verification."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..core import handover
from .client import ControlClient
from .gitops import Git
from .notes import note


def _existing_pr(api: ControlClient, claimed, repo: str, branch: str) -> dict | None:
    found = api.call("GET", "/v1/github/pr", params={"run_id": claimed.run_id, "fence": claimed.fence,
                                                       "repo": repo, "head": branch})
    return found if found and found.get("found") else None


def _publish_head(api: ControlClient, git: Git, claimed, worktree: Path, repo: str, branch: str) -> str:
    credential = api.call("POST", "/v1/github/token", {"run_id": claimed.run_id, "fence": claimed.fence,
                                                         "repo": repo, "branch": branch})
    git.push(worktree, branch, credential["token"])
    return git.run("rev-parse", "HEAD", cwd=worktree).strip()


def _pr_body(claimed, handed: dict, changed: list[str], tests: str) -> str:
    note_text = ("The full suite did not finish before this run's hard limit. CI checks must finish the suite; "
                 f"the available changes were handed over as {claimed.ticket['key']}.")
    return (f"Jira: {claimed.ticket['key']}\n\n## Summary\n{note_text}\n\n## Tests\n"
            f"{tests or 'The full suite was not finished before the developer run limit. '
              'The pull request pipeline must complete the configured checks before merge.'}\n\n"
            f"{handover.pr_sections(handed)}"
            f"## Run\n- account: {claimed.account} ({claimed.provider}"
            f"{'/' + claimed.model if claimed.model else ''})\n"
            f"- run: {claimed.run_id}, fence {claimed.fence}\n- files changed: {len(changed)}\n")


def _open_or_reuse_pr(api: ControlClient, claimed, repo: str, branch: str, existing: dict | None,
                      body: str) -> dict:
    from .dev import pr_title
    if existing:
        return existing
    return api.call("POST", "/v1/github/pr", {
        "run_id": claimed.run_id, "fence": claimed.fence, "repo": repo, "head": branch,
        "base": claimed.project["base"], "title": pr_title(claimed.ticket), "body": body, "draft": True,
    })


def try_handover(api: ControlClient, claimed, git: Git, worktree: Path, branch: str, repo: str,
                 summary: str, handed: dict, result: dict, started: float, jira: Callable[..., Any],
                 branch_commit_count: int, tests: str = "") -> bool:
    """Commit available work and send the ticket to In Verify. Return False when there is nothing to keep."""
    from .dev import api_node, commit_message
    changed = git.changed(worktree)
    existing = _existing_pr(api, claimed, repo, branch)
    if not changed and not existing and not branch_commit_count:
        return False
    if changed:
        git.commit_all(worktree, commit_message(claimed, (summary or "Developer run reached its hard time limit")[:500]),
                       claimed.identity)
        sha = _publish_head(api, git, claimed, worktree, repo, branch)
    elif branch_commit_count:
        sha = _publish_head(api, git, claimed, worktree, repo, branch)
    else:
        sha = existing.get("head_sha") or git.run("rev-parse", "HEAD", cwd=worktree).strip()
    pr = _open_or_reuse_pr(api, claimed, repo, branch, existing, _pr_body(claimed, handed, changed, tests))
    note_text = (f"The full suite did not finish before this run's hard limit. CI checks must finish the suite; "
                 f"the available changes were handed over as {claimed.ticket['key']} on {branch}. {pr['url']}")
    jira("comment", body=note(claimed, "full suite handed to pipeline",
                              what="The full suite did not finish before the developer run limit",
                              why="The run preserved its available change and opened or reused a draft pull request",
                              result=note_text, next_="The pull request pipeline completes the configured checks before merge",
                              t0=started, node=api_node(api)))
    jira("transition", to="In Verify")
    result.update(outcome="timeout", error_kind="timeout", pr_url=pr["url"], head_sha=sha, summary=note_text[:1000])
    result.setdefault("details", {}).update(
        full_suite={"status": "unfinished", "reason": "developer run reached its hard time limit"},
        handed_to_pipeline=True)
    return True
