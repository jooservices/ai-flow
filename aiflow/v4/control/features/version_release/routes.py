"""Routes of the version_release feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from types import SimpleNamespace
from typing import Any

from fastapi import Depends

from . import candidate_steps, claim_steps, completion_steps
from .bodies import PostMergeCompleteBody, VersionReleaseClaimBody, VersionReleaseCompleteBody


def install(app, ctx, deps) -> SimpleNamespace:
    def _releases():
        from ...api import RELEASES  # noqa: PLC0415 - read when a route runs, so tests may patch it
        return RELEASES

    from ...api import log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import github_gateway, github_gateway_for_connection  # noqa: PLC0415 - api imports this module
    offer = _install_offer_and_claim_routes(app, ctx, deps, log, _releases, github_gateway)
    completion = _install_completion_routes(app, ctx, deps, log, github_gateway, github_gateway_for_connection)
    return SimpleNamespace(version_release_candidates=offer.version_release_candidates,
                           version_release_claim=offer.version_release_claim,
                           version_release_complete=completion.version_release_complete,
                           environment_post_merge_complete=completion.environment_post_merge_complete)


def _install_offer_and_claim_routes(app, ctx, deps, log, releases, github_gateway) -> SimpleNamespace:
    account_master_paused = deps.account_master_paused
    bot_identity = deps.bot_identity
    node_auth = deps.node_auth
    quota_peer_release_candidates = deps.quota_peer_release_candidates

    # --- configured Jira version releases ------------------------------------------------------------------
    @app.get("/v1/version-releases/candidates")
    def version_release_candidates(account: str, node: str = Depends(node_auth)) -> dict:
        refusal, state, repo, node_row = candidate_steps.early_refusal(
            ctx, account, node, releases, account_master_paused)
        if refusal is not None:
            return refusal
        return candidate_steps.offered_versions(ctx, account, node, state, repo, node_row,
                                                quota_peer_release_candidates)

    @app.post("/v1/version-releases/claim")
    def version_release_claim(body: VersionReleaseClaimBody, node: str = Depends(node_auth)) -> Any:
        load_capacity, state = claim_steps.check_claim_policy(ctx, body, node, releases)
        repo, project, acc = claim_steps.check_claim_owner(ctx, body, node, state, account_master_paused)
        claim_steps.check_claim_node(ctx, repo, project, body, node)
        project, version = claim_steps.resolve_claim_version(ctx, body, project)
        claim_steps.check_claim_runtime(ctx, repo, project, version, node, body.account,
                                        quota_peer_release_candidates)
        claim_github, snapshot = claim_steps.evaluate_claim_gate(ctx, project, version, github_gateway)
        started = claim_steps.start_release_run(ctx, project, version, snapshot, acc, node, load_capacity,
                                                github_gateway)
        claim_steps.announce_claim(ctx, project, version, snapshot, acc, node, started)
        return claim_steps.claim_response(ctx, log, project, version, snapshot, acc, started, bot_identity)

    return SimpleNamespace(version_release_candidates=version_release_candidates,
                           version_release_claim=version_release_claim)


def _install_completion_routes(app, ctx, deps, log, github_gateway, github_gateway_for_connection) -> SimpleNamespace:
    fenced = deps.fenced
    node_auth = deps.node_auth

    @app.post("/v1/version-releases/{version_id}/complete")
    def version_release_complete(version_id: str, body: VersionReleaseCompleteBody,
                                 node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "version_release", "complete",
                     {"version_id": version_id, "head_sha": body.head_sha, "checks": body.checks})
        project, version = completion_steps.release_run_and_version(ctx, run, node, version_id)
        report = completion_steps.completion_report(project, body)
        selected_github, connection_id = completion_steps.release_gateway(
            ctx, run, project, github_gateway, github_gateway_for_connection)
        return completion_steps.complete_release(ctx, log, run, project, version, report, body, selected_github,
                                                 connection_id)

    @app.post("/v1/environment/post-merge/complete")
    def environment_post_merge_complete(body: PostMergeCompleteBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "environment", "post_merge_complete",
                     {"checks": len(body.checks), "head_sha": body.head_sha[:12]})
        return completion_steps.record_post_merge(ctx, run, node, body)

    return SimpleNamespace(version_release_complete=version_release_complete,
                           environment_post_merge_complete=environment_post_merge_complete)
