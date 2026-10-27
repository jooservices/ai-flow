"""Validate and start a version-release worker run."""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable

from . import knowledge, licensing, rollout, shared_accounts, version_release
from .github_connections import DEFAULT_ID
from .repositories.accounts import AccountsRepository
from .repositories.nodes import NodesRepository
from .repositories.runs import RunsRepository
from .repositories.version_releases import VersionReleasesRepository
from ..core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps

log = logging.getLogger("aiflow4.version_release_claim")


class ReleaseClaimRejected(Exception):
    """A version-release request failed a claim precondition."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


@dataclass
class ReleaseClaim:
    project: Any
    account: Any
    version: dict
    snapshot: dict
    key: str


def claim(ctx, body, node: str, runtime, releases, *, account_paused: Callable,
          gateway_for_repository: Callable, connection_id: Callable, bot_identity: Callable) -> dict:
    request = _prepare_claim(ctx, body, node, runtime, releases, account_paused, gateway_for_repository)
    return _start_release(ctx, request, body, node, gateway_for_repository, connection_id, bot_identity)


def _prepare_claim(ctx, body, node: str, runtime, releases, account_paused, gateway_for_repository) -> ReleaseClaim:
    license_state = _work_license(ctx, body.account, node)
    _validate_node_policy(ctx, node, releases)
    project, account = _resolve_project_account(ctx, body, node, account_paused)
    _validate_project_license(ctx, project, license_state)
    _validate_node_assignment(ctx, project, account, node)
    version = _resolve_version(ctx, project, body)
    project, version = _resolve_repository(ctx, project, version, body.repository)
    _validate_release_runtime(ctx, project, node, body.account, version["id"], runtime)
    _validate_not_active(ctx, project, version)
    gateway = gateway_for_repository(ctx, project.repo)
    if gateway is None:
        raise ReleaseClaimRejected(503, "GitHub is not connected")
    release_ctx = replace(ctx, github=gateway)
    snapshot = _ready_snapshot(ctx, release_ctx, project, version)
    key = version_release.version_identity(project.key, version["id"])
    return ReleaseClaim(project, account, version, snapshot, key)


def _work_license(ctx, account: str, node: str):
    try:
        return licensing.enforce_work_entitlement(ctx, account, node, public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        raise ReleaseClaimRejected(409, str(exc)) from None


def _validate_node_policy(ctx, node: str, releases) -> None:
    policy = rollout.node_compatible(ctx, node, releases)
    if not policy["compatible"]:
        raise ReleaseClaimRejected(409, f"node version skew: {policy['reason']}")


def _resolve_project_account(ctx, body, node: str, account_paused):
    repo = ctx.repo()
    project, account = repo.projects.get(body.project), repo.account(body.account)
    if account_paused(body.account):
        raise ReleaseClaimRejected(409, "account is paused by master monitoring")
    if ctx.leases.cooling(body.account):
        raise ReleaseClaimRejected(409, "account quota is cooling or predicted to run out before reset")
    if not project or not version_release.enabled(project) or _is_flow_repository(project.repo):
        raise ReleaseClaimRejected(403, "project is not enabled for version releases")
    if not account or not account.runs_on(node) or body.account != account.id \
            or not ({"master", "release"} & set(account.roles or [])):
        raise ReleaseClaimRejected(403, "a master or release account assigned to this node is required")
    return project, account


def _is_flow_repository(repository: str) -> bool:
    return repository.lower().rstrip("/") == "jooservices/ai-flow"


def _validate_project_license(ctx, project, license_state) -> None:
    try:
        licensing.enforce_project_entitlement(
            ctx, project.key, public_key=ctx.settings.license_public_key, state=license_state)
    except licensing.LicenseError as exc:
        raise ReleaseClaimRejected(409, str(exc)) from None


def _validate_node_assignment(ctx, project, account, node: str) -> None:
    allowed, reason = shared_accounts.may_take(ctx.db, account, node)
    if not allowed:
        raise ReleaseClaimRejected(409, reason)
    node_row = NodesRepository(ctx.db).candidate_node(node)
    if not node_row.get("accepting_work", False):
        raise ReleaseClaimRejected(409, "machine is not accepting new work")
    if project.key not in ctx.repo().projects_for_node(
            node, node_row.get("projects") or [], node_row.get("dynamic", False)):
        raise ReleaseClaimRejected(403, "project is not assigned to this node")


def _resolve_version(ctx, project, body) -> dict:
    version = next((item for item in version_release.project_versions(ctx, project)
                    if item["id"] == body.version_id), None)
    if not version:
        raise ReleaseClaimRejected(404, "configured unreleased Jira version not found")
    repository = body.repository or version.get("repository") or project.repo
    if repository not in (version.get("repositories") or [project.repo]):
        raise ReleaseClaimRejected(422, "repository is not represented in this Jira version")
    return {**version, "repository": repository}


def _resolve_repository(ctx, project, version: dict, repository: str):
    selected = {**version, "repository": repository or version["repository"]}
    project = version_release.project_for_version(ctx, project, selected)
    if not version_release.enabled(project) or _is_flow_repository(project.repo):
        raise ReleaseClaimRejected(403, "version release is not enabled for the selected repository")
    return project, selected


def _validate_release_runtime(ctx, project, node: str, account: str, version_id: str, runtime) -> None:
    node_row = NodesRepository(ctx.db).candidate_node(node)
    containers = project.settings_for_repo(project.repo).get("containers")
    if containers and toolchain_gaps(node_row.get("tools") or {}, [CONTAINER_RUNTIME_TOOL], ctx.repo().versions):
        raise ReleaseClaimRejected(409, "safe container runtime is missing on this node")
    candidate = (project.key, str(version_id), project.repo)
    if candidate in runtime.quota_peer_release_candidates(account):
        raise ReleaseClaimRejected(409, "account quota is cooling or predicted to run out before reset")


def _validate_not_active(ctx, project, version: dict) -> None:
    row = VersionReleasesRepository(ctx.db).status(project.key, version["id"], project.repo)
    if row and row["status"] in ("checking", "released"):
        raise ReleaseClaimRejected(409, "version already has an active or completed release run")


def _ready_snapshot(ctx, release_ctx, project, version: dict) -> dict:
    snapshot = version_release.post_merge_resume(release_ctx, project, version)
    if snapshot is None:
        snapshot = version_release.evaluate(release_ctx, project, version)
    if snapshot["ready"]:
        return snapshot
    if not snapshot.get("resume_after_merge"):
        version_release.record_gate(ctx, project, version, snapshot)
    raise ReleaseClaimRejected(409, {"reason": "version release gates are not met", "tickets": snapshot["tickets"],
                                     "missing": snapshot["reasons"]})


def _start_release(ctx, request: ReleaseClaim, body, node: str, gateway_for_repository,
                   connection_id: Callable, bot_identity: Callable) -> dict:
    run_id = str(uuid.uuid4())
    lease = ctx.leases.claim(request.key, run_id, ctx.settings.lease_ms)
    if not lease.ok:
        raise ReleaseClaimRejected(409, "version release is already claimed")
    try:
        started = _create_release_run(ctx, request, body, node, run_id, lease.fence,
                                      gateway_for_repository, connection_id)
    except Exception:
        ctx.leases.release(request.key, run_id, lease.fence)
        raise
    _record_claimed_release(ctx, request, started, node, run_id, lease.fence)
    return _release_response(ctx, request, started, node, run_id, lease.fence, bot_identity)


def _create_release_run(ctx, request: ReleaseClaim, body, node: str, run_id: str, fence: int,
                        gateway_for_repository, connection_id: Callable) -> dict:
    project, account, version, snapshot = request.project, request.account, request.version, request.snapshot
    github = gateway_for_repository(ctx, project.repo)
    if github is None:
        raise ReleaseClaimRejected(503, "GitHub App is not connected")
    github_connection = connection_id() or DEFAULT_ID
    source_branch = version_release.source_branch(project)
    release_branch, source_sha, branch_created = _release_branch(
        github, project, version, snapshot, source_branch)
    tracker = version_release.tracker_for(ctx, project, version["id"], version["name"])
    selected_settings = _repository_settings(project)
    _claim_shared_run(ctx, request, node, run_id, fence)
    RunsRepository(ctx.db).save_claim_details(
        run_id, {"repository": project.repo, "repository_settings": selected_settings,
                "github_connection": github_connection})
    _save_release_state(ctx, project, version, tracker, snapshot)
    version_release._audit(ctx, project, tracker,
                           {"id": run_id, "fence": fence, "account_id": account.id,
                            "github_connection": github_connection},
                           "github-release-branch", "created" if branch_created else "existing",
                           {"branch": release_branch, "source": source_branch, "source_sha": source_sha})
    return {"github": github, "connection_id": github_connection, "source_branch": source_branch,
            "source_sha": source_sha, "release_branch": release_branch, "tracker": tracker,
            "settings": selected_settings}


def _release_branch(github, project, version: dict, snapshot: dict, source_branch: str) -> tuple[str, str, bool]:
    if snapshot.get("resume_after_merge"):
        source_sha, branch = snapshot["source_sha"], snapshot["release_branch"]
        created = not github.branch_exists(project.repo_name, branch)
        if created:
            github.create_branch_at_sha(project.repo_name, branch, snapshot["release_head_sha"])
        return branch, source_sha, created
    source_sha = github.branch_sha(project.repo_name, source_branch)
    branch = version_release.release_branch(project, version["name"], source_sha)
    created = not github.branch_exists(project.repo_name, branch)
    if created:
        github.create_branch(project.repo_name, branch, source_branch)
    return branch, source_sha, created


def _repository_settings(project) -> dict:
    return {"repo": project.repo, "base": project.base, "checks": version_release.check_commands(project),
            "stack": project.stack, "containers": bool(project.settings_for_repo(project.repo).get("containers"))}


def _claim_shared_run(ctx, request: ReleaseClaim, node: str, run_id: str, fence: int) -> None:
    account = request.account
    if not shared_accounts.claim_run(
            ctx.db, account,
            (run_id, request.key, request.project.key, "version_release", account.id,
             account.provider, "", node, fence, "")):
        raise ReleaseClaimRejected(409, f"account {account.id} has no shared session capacity on {node}")


def _save_release_state(ctx, project, version: dict, tracker: str, snapshot: dict) -> None:
    VersionReleasesRepository(ctx.db).mark_checking(project, version, tracker, snapshot)


def _record_claimed_release(ctx, request: ReleaseClaim, started: dict, node: str,
                            run_id: str, fence: int) -> None:
    account, project, version, snapshot = request.account, request.project, request.version, request.snapshot
    AccountsRepository(ctx.db).mark_working(account.id)
    ctx.events.emit("version_release.claimed", ticket=started["tracker"], project=project.key,
                    step="version_release", run_id=run_id, fence=fence, account=account.id,
                    provider=account.provider, node=node, outcome="checking", reason=version["name"],
                    data={"version_id": version["id"], "tickets": snapshot["tickets"]})


def _release_response(ctx, request: ReleaseClaim, started: dict, node: str,
                      run_id: str, fence: int, bot_identity: Callable) -> dict:
    project, account, version, snapshot = request.project, request.account, request.version, request.snapshot
    ticket = {"key": request.key, "project": project.key, "summary": f"Release {version['name']}",
              "description": f"Run full configured project checks on {started['release_branch']}, "
                             f"cut from {started['source_branch']}.",
              "labels": [], "priority": "Highest", "type": "Release"}
    project_data = _release_project_data(project, started)
    try:
        context_pack = knowledge.context_pack(ctx.db, project=project.key, ticket=request.key,
                                              query=f"{ticket['summary']}\n{ticket['description']}", run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge context pack for release %s failed: %s", version["name"], type(exc).__name__)
        context_pack = {"project": project.key, "ticket": request.key, "items": []}
    return {"claimed": True, "step": "version_release", "run_id": run_id, "fence": fence,
            "run_token": secrets.token_urlsafe(32), "lease_ms": ctx.settings.lease_ms,
            "provider": account.provider, "model": "", "effort": "", "account": account.id,
            "identity": bot_identity(), "ticket": ticket, "project": project_data, "context_pack": context_pack,
            "version": version, "tracker_key": started["tracker"], "tickets": snapshot["tickets"]}


def _release_project_data(project, started: dict) -> dict:
    return {"key": project.key, "repo": project.repo, "base": project.base,
            "checks": version_release.check_commands(project),
            "source_branch": version_release.source_branch(project),
            "target_branch": version_release.target_branch(project), "release_branch": started["release_branch"],
            "source_sha": started["source_sha"],
            "environment": [item.public() for item in project.environment_items if "post_merge" in item.runs],
            "containers": bool(project.settings_for_repo(project.repo).get("containers")),
            "work_minutes": project.work_minutes()}
