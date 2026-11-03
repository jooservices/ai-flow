"""The steps of the version release offer: refuse early, then list the versions this machine and account may take.

The steps call the policy modules directly (not through parameters) so the documentation check still sees, from the
route, which rules an offer goes through."""
from ....core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from ... import licensing, load_policy_api, rollout, system_check_api, version_release


def early_refusal(ctx, account: str, node: str, releases, account_master_paused):
    """(refusal, state, repo, node_row): a refusal answer, or None with what the listing needs."""
    if blocked := load_policy_api.blocked_offer(ctx, node, "versions"):
        return blocked, None, None, None
    try:
        state = licensing.enforce_work_entitlement(ctx, account, node, public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        return {"versions": [], "reason": str(exc)}, None, None, None
    policy = rollout.node_compatible(ctx, node, releases())
    if not policy["compatible"]:
        return {"versions": [], "reason": f"version skew: {policy['reason']}"}, None, None, None
    repo = ctx.repo()
    node_row = ctx.db.one("SELECT accepting_work,readiness,tools FROM nodes WHERE name=%s AND removed_at IS NULL", (node,))
    if not node_row or not node_row["accepting_work"]:
        return {"versions": [], "reason": "machine is not accepting new work"}, None, None, None
    if account_master_paused(account):
        return {"versions": [], "reason": "account is paused by master monitoring"}, None, None, None
    if ctx.leases.cooling(account):
        return ({"versions": [], "reason": "account quota is cooling or predicted to run out before reset"},
                None, None, None)
    return None, state, repo, node_row


def container_ready(repo, node_row: dict, versions: list[dict]) -> list[dict]:
    """Drop the versions whose project needs containers when this machine lacks the safe container runtime."""
    return [item for item in versions
            if not (item.get("project") in repo.projects
                    and repo.projects[item["project"]].settings_for_repo(item.get("repository", "")).get("containers")
                    and toolchain_gaps(node_row.get("tools") or {}, [CONTAINER_RUNTIME_TOOL], repo.versions))]


def entitled(ctx, state: dict, available: list[dict]) -> dict:
    """The versions whose project the licence allows; the first reason when none is allowed."""
    allowed, refused = [], []
    for candidate in available:
        try:
            licensing.enforce_project_entitlement(ctx, candidate["project"],
                                                  public_key=ctx.settings.license_public_key, state=state)
        except licensing.LicenseError as exc:
            refused.append(str(exc))
        else:
            allowed.append(candidate)
    result = {"versions": allowed}
    if refused and not allowed:
        result["reason"] = refused[0]
    return result


def offered_versions(ctx, account: str, node: str, state: dict, repo, node_row: dict,
                     quota_peer_release_candidates) -> dict:
    versions = version_release.candidates(ctx, account, node)
    refusal = system_check_api.release_candidate_refusal(
        node_row.get("readiness"), account, [item.id for item in repo.accounts_on(node)], versions)
    if refusal:
        return refusal
    versions = container_ready(repo, node_row, versions)
    peer_candidates = quota_peer_release_candidates(account)
    available = [candidate for candidate in versions
                 if (candidate["project"], str(candidate["version_id"]),
                     str(candidate.get("repository") or "")) not in peer_candidates]
    if versions and not available:
        return {"versions": [], "reason": "account quota is cooling or predicted to run out before reset"}
    return entitled(ctx, state, available)
