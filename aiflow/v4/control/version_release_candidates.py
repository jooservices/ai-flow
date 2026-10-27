"""Offer configured version releases to an authenticated worker account."""
from __future__ import annotations

from . import licensing, rollout, version_release
from ..core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from .repositories.nodes import NodesRepository


QUOTA_REASON = "account quota is cooling or predicted to run out before reset"


def candidates(ctx, account: str, node: str, runtime, releases, account_paused) -> dict:
    try:
        license_state = licensing.enforce_work_entitlement(
            ctx, account, node, public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        return {"versions": [], "reason": str(exc)}
    policy = rollout.node_compatible(ctx, node, releases)
    if not policy["compatible"]:
        return {"versions": [], "reason": f"version skew: {policy['reason']}"}
    node_row = NodesRepository(ctx.db).candidate_node(node)
    reason = _node_rejection(node_row, account, ctx, account_paused)
    if reason:
        return {"versions": [], "reason": reason}
    versions = _without_container_gaps(ctx, version_release.candidates(ctx, account, node), node_row)
    available = _without_quota_peers(versions, runtime.quota_peer_release_candidates(account))
    if versions and not available:
        return {"versions": [], "reason": QUOTA_REASON}
    allowed, refused = _enforce_project_licenses(ctx, available, license_state)
    result = {"versions": allowed}
    if refused and not allowed:
        result["reason"] = refused[0]
    return result


def _node_rejection(node_row: dict, account: str, ctx, account_paused) -> str:
    if not node_row or not node_row.get("accepting_work"):
        return "machine is not accepting new work"
    if account_paused(account):
        return "account is paused by master monitoring"
    if ctx.leases.cooling(account):
        return QUOTA_REASON
    return ""


def _without_container_gaps(ctx, versions: list[dict], node_row: dict) -> list[dict]:
    repo = ctx.repo()
    gaps = toolchain_gaps(node_row.get("tools") or {}, [CONTAINER_RUNTIME_TOOL], repo.versions)
    return [item for item in versions
            if not (item.get("project") in repo.projects
                    and repo.projects[item["project"]].settings_for_repo(item.get("repository", "")).get("containers")
                    and gaps)]


def _without_quota_peers(versions: list[dict], peers: set[tuple[str, str, str]]) -> list[dict]:
    return [item for item in versions
            if (item["project"], str(item["version_id"]), str(item.get("repository") or "")) not in peers]


def _enforce_project_licenses(ctx, versions: list[dict], license_state) -> tuple[list[dict], list[str]]:
    allowed, refused = [], []
    for candidate in versions:
        try:
            licensing.enforce_project_entitlement(
                ctx, candidate["project"], public_key=ctx.settings.license_public_key, state=license_state)
        except licensing.LicenseError as exc:
            refused.append(str(exc))
        else:
            allowed.append(candidate)
    return allowed, refused
