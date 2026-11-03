"""Routes of the github_fenced feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from dataclasses import dataclass
from datetime import timedelta
import csv
import hashlib
import hmac
import httpx
import io
import json
import re
from types import SimpleNamespace

from .bodies import BranchCleanupBody, GitHubTokenBody, MergeBody, PRBody, ReviewerBody
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import (PlainTextResponse, Response)
from ... import (escalations,
               version_release)
from ... import merge_queue, review_queue_api
from ... import merge_flow
from ...route_deps import github_connection_used
from ...github import GitHubError
from ... import pr_comments
from ...pr_comments import PRCommentBody
from ...features.work.constants import utcnow
from ...platform.events.wiring import publish_for
from .events import (GitHubBranchDeleted, GitHubBranchUpdated, GitHubCommentPosted, GitHubGitRelayTokenIssued,
                     GitHubPrClosed, GitHubPrCreated, GitHubPrLookedUp, GitHubPushAccepted, GitHubTokenIssued)

__all__ = [
    "BranchCleanupBody", "Depends", "GitHubBranchDeleted", "GitHubBranchUpdated", "GitHubCommentPosted",
    "GitHubError", "GitHubGitRelayTokenIssued", "GitHubPrClosed", "GitHubPrCreated", "GitHubPrLookedUp",
    "GitHubPushAccepted", "GitHubTokenBody", "GitHubTokenIssued", "HTTPException", "MergeBody",
    "PRBody", "PRCommentBody", "PlainTextResponse", "Query", "Request", "Response", "ReviewerBody",
    "csv", "escalations", "github_connection_used", "hashlib", "hmac", "httpx", "io", "json",
    "merge_flow", "merge_queue", "pr_comments", "publish_for", "re", "review_queue_api", "timedelta",
    "utcnow", "version_release",
]


def _split_git_path(git_path: str) -> tuple[str, str, str]:
    """Split `owner/name.git/<operation path>` at the LAST `.git/`, so a repository name that itself contains `.git`
    (for example `ai.github.io`) is kept whole. Returns (repository, ".git" or "", the rest after ".git")."""
    index = git_path.rfind(".git/")
    if index < 0 and git_path.endswith(".git"):
        index = len(git_path) - len(".git")
    if index < 0:
        return git_path, "", ""
    return git_path[:index], ".git", git_path[index + len(".git"):]


def _git_transfer_request(git_path: str, request: Request, capability: dict) -> tuple[str, str, str]:
    """Resolve the repository, path suffix and Git operation of one relay request."""
    repository, separator, suffix = _split_git_path(git_path)
    if (not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or repository.lower() != str(capability.get("repo", ""))):
        raise HTTPException(403, "Git repository transfer is outside this credential's scope")
    suffix = ".git" + suffix
    if request.method == "GET" and suffix == ".git/info/refs":
        service = request.query_params.get("service", "")
        if service not in ("git-upload-pack", "git-receive-pack"):
            raise HTTPException(400, "unsupported Git service")
        return repository, suffix, service
    if request.method == "POST" and suffix in (".git/git-upload-pack", ".git/git-receive-pack"):
        return repository, suffix, suffix.rsplit("/", 1)[-1]
    raise HTTPException(404, "unsupported Git repository transfer operation")


def _authorize_check_probe(capability: dict, repository: str, operation: str,
                           fleet_routes) -> tuple[None, str, int]:
    """A check-probe capability reads the configured repositories of the project it names, nothing else."""
    if operation != "git-upload-pack":
        raise HTTPException(403, "check-probe Git credentials are read-only")
    node_name = str(capability.get("node") or "")
    project_key = str(capability.get("project") or "")
    project = fleet_routes.probe_project(node_name, project_key)
    if repository.lower() not in {name.lower() for name in project.configured_repositories}:
        raise HTTPException(409, "check-probe repository is no longer configured")
    return None, "", -1


def _authorize_run_transfer(capability: dict, repository: str, operation: str, ctx, run_row,
                            matches_run_repository, repository_for_run) -> tuple[dict, str, int]:
    """A run capability transfers only the repository and branch of a live, fenced run."""
    run_id = str(capability.get("run_id") or "")
    run = run_row(run_id)
    fence = int(capability.get("fence", -1))
    if (run["node"] != capability.get("node") or int(run["fence"]) != fence
            or not ctx.leases.valid(run["ticket_key"], run_id, fence)
            or not matches_run_repository(repository_for_run(run, ctx.repo().projects.get(run["project"])),
                                          repository)):
        raise HTTPException(409, "Git repository transfer run is no longer active")
    if operation == "git-receive-pack" and not capability.get("branch"):
        raise HTTPException(403, "Git push is not allowed by this read-only credential")
    return run, run_id, fence


def _authorized_git_transfer(capability: dict, repository: str, operation: str, *, ctx, fleet_routes, run_row,
                             matches_run_repository, repository_for_run) -> tuple[dict | None, str, int]:
    """Dispatch on the capability purpose; every refusal keeps its original status and message."""
    purpose = str(capability.get("purpose") or "run")
    if purpose == "check_probe":
        return _authorize_check_probe(capability, repository, operation, fleet_routes)
    if purpose == "run":
        return _authorize_run_transfer(capability, repository, operation, ctx, run_row,
                                       matches_run_repository, repository_for_run)
    raise HTTPException(403, "Git repository transfer credential has an unsupported purpose")

def _github_git_upstream(*args, **kwargs):
    from ... import gateway_helpers as _helpers  # noqa: PLC0415 - looked up when called, so tests may patch it
    return _helpers._github_git_upstream(*args, **kwargs)


@dataclass(frozen=True)
class _RouteRuntime:
    log: object
    git_proxy_credential: object
    issue_git_proxy_token: object
    receive_pack_target: object
    verify_git_proxy_token: object
    github_gateway: object
    github_gateway_for_connection: object


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import (_git_proxy_credential, _issue_git_proxy_token, _receive_pack_target,
                                    _verify_git_proxy_token, github_gateway,
                                    github_gateway_for_connection)  # noqa: PLC0415
    from .routes_access import install_github_token_routes, install_git_proxy_routes  # noqa: PLC0415
    from .routes_pull_requests import install_github_merge_routes, install_pull_request_routes  # noqa: PLC0415
    from .routes_reviews import (install_review_answer_and_gate_routes, install_review_approval_routes,
                                 install_review_export_routes, install_review_queue_routes,
                                 install_review_rejection_and_question_routes)  # noqa: PLC0415
    from .routes_maintenance import install_github_maintenance_routes  # noqa: PLC0415

    runtime = _RouteRuntime(
        log, _git_proxy_credential, _issue_git_proxy_token, _receive_pack_target,
        _verify_git_proxy_token, github_gateway, github_gateway_for_connection)
    github_token = install_github_token_routes(app, ctx, deps, runtime)
    install_git_proxy_routes(app, ctx, deps, runtime)
    github_prs = install_pull_request_routes(app, ctx, deps, runtime)
    github_merge = install_github_merge_routes(app, ctx, deps, runtime)
    reviews = install_review_queue_routes(app, ctx, deps, runtime)
    export_review_metrics = install_review_export_routes(app, ctx, deps, reviews.review_data)
    review_decisions = install_review_approval_routes(app, ctx, deps, runtime)
    review_followups = install_review_rejection_and_question_routes(
        app, ctx, deps, runtime, reviews.review_item_kind, reviews.review_thread)
    review_answers = install_review_answer_and_gate_routes(
        app, ctx, deps, runtime, reviews.review_item_kind)
    github_maintenance = install_github_maintenance_routes(app, ctx, deps, runtime)
    return SimpleNamespace(
        github_token=github_token, github_find_pr=github_prs.github_find_pr, github_pr=github_prs.github_pr,
        github_merge=github_merge, review_queue=reviews.review_queue, review_item_kind=reviews.review_item_kind,
        current_review_item_kind=reviews.current_review_item_kind, review_thread=reviews.review_thread,
        review_data=reviews.review_data, export_review_metrics=export_review_metrics,
        approve_review=review_decisions.approve_review,
        request_review_rework=review_decisions.request_review_rework,
        reject_review=review_followups.reject_review, ask_review_ai=review_followups.ask_review_ai,
        answer_review_question=review_answers.answer_review_question,
        skip_review_gate=review_answers.skip_review_gate,
        github_update_branch=github_maintenance.github_update_branch,
        github_checks=github_maintenance.github_checks, github_comment=github_maintenance.github_comment,
        github_delete_branch=github_maintenance.github_delete_branch)
