"""Pull-request resolution and retry state for finished-ticket worktree cleanup."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .github import GitHub, GitHubError
from .gitops import GitError
from .jira import JiraError

CLEANUP_MARKER_TTL = 365 * 86400


def resolve_merged_pull_request(
    repo: Any,
    key: str,
    branch: str,
    known_pr: Callable[[str, GitHub], dict[str, Any] | None],
) -> tuple[bool, Exception | None]:
    """Find a merged PR by head branch or by the ticket's recorded PR, retaining lookup errors for diagnostics."""
    lookup_error = None
    try:
        gh = GitHub(repo.github_slug())
    except (GitError, GitHubError, JiraError, ValueError, KeyError) as error:
        return False, error

    try:
        pr = gh.find(branch)
    except (GitError, GitHubError, JiraError, ValueError, KeyError) as error:
        lookup_error = error
        pr = None
    if pr is None:
        try:
            recorded = known_pr(key, gh)
            if recorded and recorded.get("headRefName") == branch:
                pr = recorded
        except (GitError, GitHubError, JiraError, ValueError, KeyError) as error:
            lookup_error = error
    return bool(pr and pr.get("state") == "MERGED"), lookup_error


def cleanup_done_worktree(redis: Any, repo: Any, key: str, branch: str, merged_pr: bool,
                          lookup_error: Exception | None, logger: Callable[[str], None]) -> None:
    """Clean a finished ticket once, remembering failures until a confirmed merge makes retry safe."""
    marker = f"sweep-cleanup:{key}:{branch}"
    state = redis.get(marker)
    if state == "stopped" or (state == "awaiting-merge" and not merged_pr):
        return
    try:
        repo.cleanup(key, merged_pr=merged_pr)
        redis.delete(marker)
        logger(f"{key}: removed the worktree and branch of a finished ticket")
    except (GitError, OSError) as error:
        awaiting_merge = not merged_pr and "not fully merged" in str(error).casefold()
        state = "awaiting-merge" if awaiting_merge else "stopped"
        redis.set(marker, state, CLEANUP_MARKER_TTL)
        reason = f"cleanup failed ({error})"
        if lookup_error:
            reason += f"; cannot check pull request before cleanup ({lookup_error})"
        if awaiting_merge:
            reason += "; will retry after GitHub confirms the PR is merged"
        logger(f"{key}: keeping the worktree and branch; {reason}")
