"""Named phases used by the version-release claim route."""


def check_claim_policy(ctx, body, node, capacity_lookup, enforce_work, license_error, node_compatible, releases,
                       http_exception):
    """Load claim capacity and enforce the worker license and node release policy."""
    capacity = capacity_lookup(ctx, node)
    try:
        state = enforce_work(ctx, body.account, node, public_key=ctx.settings.license_public_key)
    except license_error as exc:
        raise http_exception(409, str(exc)) from None
    policy = node_compatible(ctx, node, releases())
    if not policy["compatible"]:
        raise http_exception(409, f"node version skew: {policy['reason']}")
    return capacity, state


def check_claim_owner(ctx, body, node, state, account_paused, enforce_project, license_error, may_take,
                      project_enabled, http_exception):
    """Validate project, account, entitlement and shared-session eligibility in route order."""
    repo = ctx.repo()
    project = repo.projects.get(body.project)
    account = repo.account(body.account)
    if account_paused(body.account):
        raise http_exception(409, "account is paused by master monitoring")
    if ctx.leases.cooling(body.account):
        raise http_exception(409, "account quota is cooling or predicted to run out before reset")
    if (not project or not project_enabled(project)
            or project.repo.lower().rstrip("/") == "jooservices/ai-flow"):
        raise http_exception(403, "project is not enabled for version releases")
    if not account or not account.runs_on(node) or body.account != account.id or not ({"master", "release"} & set(account.roles or [])):
        raise http_exception(403, "a master or release account assigned to this node is required")
    try:
        enforce_project(ctx, project.key, public_key=ctx.settings.license_public_key, state=state)
    except license_error as exc:
        raise http_exception(409, str(exc)) from None
    can_take, reason = may_take(ctx.db, account, node)
    if not can_take:
        raise http_exception(409, reason)
    return repo, project, account


def check_claim_node(ctx, repo, project, body, node, require_project_ready, http_exception):
    """Check node acceptance, project assignment and existing readiness evidence."""
    node_row = ctx.db.one("SELECT projects,dynamic,status,accepting_work,readiness FROM nodes WHERE name=%s", (node,)) or {}
    if not node_row.get("accepting_work", False):
        raise http_exception(409, "machine is not accepting new work")
    assigned = repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False))
    if project.key not in assigned:
        raise http_exception(403, "project is not assigned to this node")
    require_project_ready(node_row.get("readiness"), project.key, body.account,
                          [item.id for item in repo.accounts_on(node)])
    return node_row


def resolve_claim_version(ctx, body, project, project_versions, project_for_version, project_enabled,
                          http_exception):
    """Find the configured Jira version and resolve its requested repository project."""
    version = next((item for item in project_versions(ctx, project) if item["id"] == body.version_id), None)
    if not version:
        raise http_exception(404, "configured unreleased Jira version not found")
    requested_repository = body.repository or version.get("repository") or project.repo
    if requested_repository not in (version.get("repositories") or [project.repo]):
        raise http_exception(422, "repository is not represented in this Jira version")
    version = {**version, "repository": requested_repository}
    project = project_for_version(ctx, project, version)
    if not project_enabled(project) or project.repo.lower().rstrip("/") == "jooservices/ai-flow":
        raise http_exception(403, "version release is not enabled for the selected repository")
    return project, version


def check_claim_runtime(ctx, repo, project, version, node, account_id, toolchain_gaps, container_runtime,
                        peer_candidates, http_exception):
    """Refuse unsupported container nodes, peer quota conflicts and duplicate release states."""
    if (project.settings_for_repo(project.repo).get("containers")
            and toolchain_gaps((ctx.db.one("SELECT tools FROM nodes WHERE name=%s", (node,)) or {}).get("tools") or {},
                               [container_runtime], repo.versions)):
        raise http_exception(409, "safe container runtime is missing on this node")
    candidate_key = (project.key, str(version["id"]), project.repo)
    if candidate_key in peer_candidates(account_id):
        raise http_exception(409, "account quota is cooling or predicted to run out before reset")
    state = ctx.db.one("SELECT status FROM version_releases WHERE project=%s AND version_id=%s AND repository=%s",
                       (project.key, version["id"], project.repo))
    if state and state["status"] in ("checking", "released"):
        raise http_exception(409, "version already has an active or completed release run")


def evaluate_claim_gate(ctx, project, version, gateway_lookup, replace_context, post_merge_resume, evaluate,
                        record_gate, http_exception):
    """Load GitHub and evaluate or resume the Jira release gates before taking a lease."""
    claim_github = gateway_lookup(ctx, project.repo)
    if claim_github is None:
        raise http_exception(503, "GitHub is not connected")
    release_ctx = replace_context(ctx, github=claim_github)
    snapshot = post_merge_resume(release_ctx, project, version)
    if snapshot is None:
        snapshot = evaluate(release_ctx, project, version)
    if not snapshot["ready"]:
        if not snapshot.get("resume_after_merge"):
            record_gate(ctx, project, version, snapshot)
        raise http_exception(409, {"reason": "version release gates are not met", "tickets": snapshot["tickets"],
                                   "missing": snapshot["reasons"]})
    return claim_github, snapshot


def create_claim_branch(project, version, snapshot, github, source_branch_for, release_branch_for):
    """Create or reuse the normal or post-merge release branch and return its audit values."""
    source_branch = source_branch_for(project)
    if snapshot.get("resume_after_merge"):
        source_sha = snapshot["source_sha"]
        release_branch = snapshot["release_branch"]
        branch_created = not github.branch_exists(project.repo_name, release_branch)
        if branch_created:
            github.create_branch_at_sha(project.repo_name, release_branch, snapshot["release_head_sha"])
    else:
        source_sha = github.branch_sha(project.repo_name, source_branch)
        release_branch = release_branch_for(project, version["name"], source_sha)
        branch_created = not github.branch_exists(project.repo_name, release_branch)
        if branch_created:
            github.create_branch(project.repo_name, release_branch, source_branch)
    return source_branch, source_sha, release_branch, branch_created


def persist_claim_rows(ctx, project, version, run_id, tracker, selected_settings, connection_id, snapshot, json_value):
    """Store the run's repository settings and the version's checking snapshot."""
    ctx.db.run("UPDATE runs SET details=%s WHERE id=%s",
               (json_value({"repository": project.repo, "repository_settings": selected_settings,
                            "github_connection": connection_id}), run_id))
    ctx.db.run("INSERT INTO version_releases (project,version_id,version_name,tracker_key,status,gate_detail,ticket_snapshot,repository)"
               " VALUES (%s,%s,%s,%s,'checking','',%s,%s)"
               " ON CONFLICT (project,version_id,repository) DO UPDATE SET"
               " version_name=EXCLUDED.version_name,tracker_key=EXCLUDED.tracker_key,status='checking',"
               " gate_detail='',ticket_snapshot=EXCLUDED.ticket_snapshot,updated_at=now()",
               (project.key, version["id"], version["name"], tracker, json_value(snapshot["tickets"]), project.repo))
