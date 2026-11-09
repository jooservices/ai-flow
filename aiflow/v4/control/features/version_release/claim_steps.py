"""Named phases used by the version-release claim route.

The steps call the policy modules directly (not through parameters), so the documentation check still sees, from the
route, which entitlement, quota and release rules a claim goes through."""
import secrets
import uuid
from dataclasses import dataclass, replace

from fastapi import HTTPException

from ....core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from ... import licensing, load_policy_api, rollout, shared_accounts, system_check_api, version_release
from ...db import J
from ...github_connections import DEFAULT_ID
from ...route_deps import github_connection_used


def check_claim_policy(ctx, body, node, releases):
    """Load claim capacity and enforce the worker license and node release policy."""
    capacity = load_policy_api.version_claim_capacity(ctx, node)
    try:
        state = licensing.enforce_work_entitlement(ctx, body.account, node, public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        raise HTTPException(409, str(exc)) from None
    policy = rollout.node_compatible(ctx, node, releases())
    if not policy["compatible"]:
        raise HTTPException(409, f"node version skew: {policy['reason']}")
    return capacity, state


def check_claim_owner(ctx, body, node, state, account_paused):
    """Validate project, account, entitlement and shared-session eligibility in route order."""
    repo = ctx.repo()
    project = repo.projects.get(body.project)
    account = repo.account(body.account)
    if account_paused(body.account):
        raise HTTPException(409, "account is paused by master monitoring")
    if ctx.leases.cooling(body.account):
        raise HTTPException(409, "account quota is cooling or predicted to run out before reset")
    if (not project or not version_release.enabled(project)
            or project.repo.lower().rstrip("/") == "jooservices/ai-flow"):
        raise HTTPException(403, "project is not enabled for version releases")
    if not account or not account.runs_on(node) or body.account != account.id or not ({"master", "release"} & set(account.roles or [])):
        raise HTTPException(403, "a master or release account assigned to this node is required")
    try:
        licensing.enforce_project_entitlement(ctx, project.key, public_key=ctx.settings.license_public_key, state=state)
    except licensing.LicenseError as exc:
        raise HTTPException(409, str(exc)) from None
    can_take, reason = shared_accounts.may_take(ctx.db, account, node)
    if not can_take:
        raise HTTPException(409, reason)
    return repo, project, account


def check_claim_node(ctx, repo, project, body, node):
    """Check node acceptance, project assignment and existing readiness evidence."""
    node_row = ctx.db.one("SELECT projects,dynamic,status,accepting_work,readiness FROM nodes WHERE name=%s", (node,)) or {}
    if not node_row.get("accepting_work", False):
        raise HTTPException(409, "machine is not accepting new work")
    assigned = repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False))
    if project.key not in assigned:
        raise HTTPException(403, "project is not assigned to this node")
    system_check_api.require_project_ready(node_row.get("readiness"), project.key, body.account,
                          [item.id for item in repo.accounts_on(node)])
    return node_row


def resolve_claim_version(ctx, body, project):
    """Find the configured Jira version and resolve its requested repository project."""
    version = next((item for item in version_release.project_versions(ctx, project) if item["id"] == body.version_id), None)
    if not version:
        raise HTTPException(404, "configured unreleased Jira version not found")
    requested_repository = body.repository or version.get("repository") or project.repo
    if requested_repository not in (version.get("repositories") or [project.repo]):
        raise HTTPException(422, "repository is not represented in this Jira version")
    version = {**version, "repository": requested_repository}
    project = version_release.project_for_version(ctx, project, version)
    if not version_release.enabled(project) or project.repo.lower().rstrip("/") == "jooservices/ai-flow":
        raise HTTPException(403, "version release is not enabled for the selected repository")
    return project, version


def check_claim_runtime(ctx, repo, project, version, node, account_id, peer_candidates):
    """Refuse unsupported container nodes, peer quota conflicts and duplicate release states."""
    if (project.settings_for_repo(project.repo).get("containers")
            and toolchain_gaps((ctx.db.one("SELECT tools FROM nodes WHERE name=%s", (node,)) or {}).get("tools") or {},
                               [CONTAINER_RUNTIME_TOOL], repo.versions)):
        raise HTTPException(409, "safe container runtime is missing on this node")
    candidate_key = (project.key, str(version["id"]), project.repo)
    if candidate_key in peer_candidates(account_id):
        raise HTTPException(409, "account quota is cooling or predicted to run out before reset")
    state = ctx.db.one("SELECT status FROM version_releases WHERE project=%s AND version_id=%s AND repository=%s",
                       (project.key, version["id"], project.repo))
    if state and state["status"] in ("checking", "released"):
        raise HTTPException(409, "version already has an active or completed release run")


def evaluate_claim_gate(ctx, project, version, gateway_lookup):
    """Load GitHub and evaluate or resume the Jira release gates before taking a lease."""
    claim_github = gateway_lookup(ctx, project.repo)
    if claim_github is None:
        raise HTTPException(503, "GitHub is not connected")
    release_ctx = replace(ctx, github=claim_github)
    snapshot = version_release.post_merge_resume(release_ctx, project, version)
    if snapshot is None:
        snapshot = version_release.evaluate(release_ctx, project, version)
    if not snapshot["ready"]:
        if not snapshot.get("resume_after_merge"):
            version_release.record_gate(ctx, project, version, snapshot)
        raise HTTPException(409, {"reason": "version release gates are not met", "tickets": snapshot["tickets"],
                                   "missing": snapshot["reasons"]})
    return claim_github, snapshot


def create_claim_branch(project, version, snapshot, github):
    """Create or reuse the normal or post-merge release branch and return its audit values."""
    source_branch = version_release.source_branch(project)
    if snapshot.get("resume_after_merge"):
        source_sha = snapshot["source_sha"]
        release_branch = snapshot["release_branch"]
        branch_created = not github.branch_exists(project.repo_name, release_branch)
        if branch_created:
            github.create_branch_at_sha(project.repo_name, release_branch, snapshot["release_head_sha"])
    else:
        source_sha = github.branch_sha(project.repo_name, source_branch)
        release_branch = version_release.release_branch(project, version["name"], source_sha)
        branch_created = not github.branch_exists(project.repo_name, release_branch)
        if branch_created:
            github.create_branch(project.repo_name, release_branch, source_branch)
    return source_branch, source_sha, release_branch, branch_created


def persist_claim_rows(ctx, project, version, run_id, tracker, selected_settings, connection_id, snapshot):
    """Store the run's repository settings and the version's checking snapshot."""
    ctx.db.run("UPDATE runs SET details=%s WHERE id=%s",
               (J({"repository": project.repo, "repository_settings": selected_settings,
                            "github_connection": connection_id}), run_id))
    ctx.db.run("INSERT INTO version_releases (project,version_id,version_name,tracker_key,status,gate_detail,ticket_snapshot,repository)"
               " VALUES (%s,%s,%s,%s,'checking','',%s,%s)"
               " ON CONFLICT (project,version_id,repository) DO UPDATE SET"
               " version_name=EXCLUDED.version_name,tracker_key=EXCLUDED.tracker_key,status='checking',"
               " gate_detail='',ticket_snapshot=EXCLUDED.ticket_snapshot,updated_at=now()",
               (project.key, version["id"], version["name"], tracker, J(snapshot["tickets"]), project.repo))


@dataclass(frozen=True)
class StartedRelease:
    """What taking the lease and preparing the release branch produced."""
    key: str
    run_id: str
    lease: object
    tracker: str
    source_branch: str
    source_sha: str
    release_branch: str


def start_release_run(ctx, project, version, snapshot, acc, node, load_capacity, github_gateway) -> StartedRelease:
    """Take the version's lease, prepare the branch and store the run; the lease is released if anything fails."""
    key = version_release.version_identity(project.key, version["id"])
    run_id = str(uuid.uuid4())
    lease = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
    if not lease.ok:
        raise HTTPException(409, "version release is already claimed")
    try:
        github = github_gateway(ctx, project.repo)
        if github is None:
            raise HTTPException(503, "GitHub App is not connected")
        connection_id = github_connection_used.get() or DEFAULT_ID
        source_branch, source_sha, release_branch, branch_created = create_claim_branch(
            project, version, snapshot, github)
        tracker = version_release.tracker_for(ctx, project, version["id"], version["name"])
        selected_settings = {"repo": project.repo, "base": project.base,
                             "checks": version_release.check_commands(project), "stack": project.stack,
                             "containers": bool(project.settings_for_repo(project.repo).get("containers"))}
        claim_values = (run_id, key, project.key, "version_release", acc.id, acc.provider, "", node, lease.fence, "")
        if not load_policy_api.claim_run(ctx, acc, claim_values, load_capacity):
            raise HTTPException(409, f"account {acc.id} has no shared session capacity on {node}")
        persist_claim_rows(ctx, project, version, run_id, tracker, selected_settings, connection_id, snapshot)
        version_release._audit(ctx, project, tracker,
                               {"id": run_id, "fence": lease.fence, "account_id": acc.id,
                                "github_connection": connection_id},
                               "github-release-branch", "created" if branch_created else "existing",
                               {"branch": release_branch, "source": source_branch, "source_sha": source_sha})
    except Exception:  # noqa: BLE001 - any setup failure must release the claimed version lease
        ctx.leases.release(key, run_id, lease.fence)
        raise
    return StartedRelease(key, run_id, lease, tracker, source_branch, source_sha, release_branch)


def announce_claim(ctx, project, version, snapshot, acc, node, started: StartedRelease) -> None:
    ctx.db.run("UPDATE accounts SET status='working',updated_at=now(),idle_since=NULL,cooldown_since=NULL WHERE id=%s",
               (acc.id,))
    ctx.events.emit("version_release.claimed", ticket=started.tracker, project=project.key, step="version_release",
                    run_id=started.run_id, fence=started.lease.fence, account=acc.id, provider=acc.provider,
                    node=node, outcome="checking", reason=version["name"],
                    data={"version_id": version["id"], "tickets": snapshot["tickets"]})


def claim_response(ctx, log, project, version, snapshot, acc, started: StartedRelease, bot_identity) -> dict:
    """The run description the worker receives: ticket, project settings and the knowledge context pack."""
    key, run_id = started.key, started.run_id
    project_data = {"key": project.key, "repo": project.repo, "base": project.base,
                    "checks": version_release.check_commands(project),
                    "source_branch": version_release.source_branch(project),
                    "target_branch": version_release.target_branch(project),
                    "release_branch": started.release_branch, "source_sha": started.source_sha,
                    "environment": ([item.public() for item in project.environment_items
                                     if "post_merge" in item.runs]),
                    "containers": bool(project.settings_for_repo(project.repo).get("containers")),
                    "work_minutes": project.work_minutes()}
    ticket = {"key": key, "project": project.key, "summary": f"Release {version['name']}",
              "description": (f"Run full configured project checks on {started.release_branch}, "
                             f"cut from {started.source_branch}."),
              "labels": [], "priority": "Highest", "type": "Release"}
    context_pack = {"project": project.key, "ticket": key, "items": []}
    return {"claimed": True, "step": "version_release", "run_id": run_id, "fence": started.lease.fence,
            "run_token": secrets.token_urlsafe(32), "lease_ms": ctx.settings.lease_ms,
            "provider": acc.provider, "model": "", "effort": "", "account": acc.id,
            "identity": bot_identity(), "ticket": ticket, "project": project_data, "context_pack": context_pack,
            "version": version, "tracker_key": started.tracker, "tickets": snapshot["tickets"]}
