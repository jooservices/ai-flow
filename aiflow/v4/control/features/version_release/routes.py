"""Routes of the version_release feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import secrets
import uuid
from dataclasses import replace
from types import SimpleNamespace

from .bodies import PostMergeCompleteBody, VersionReleaseClaimBody, VersionReleaseCompleteBody
from . import claim_steps
from typing import Any
from fastapi import Depends, HTTPException
from ....core.toolchains import (CONTAINER_RUNTIME_TOOL, toolchain_gaps)
from ... import (knowledge, licensing, load_policy_api,
               rollout, shared_accounts, system_check_api, version_release)
from ... import environment as environment_reports
from ...db import J
from ...route_deps import github_connection_used
from ...github_connections import DEFAULT_ID


def install(app, ctx, deps) -> SimpleNamespace:
    def _releases():
        from ...api import RELEASES  # noqa: PLC0415 - read when a route runs, so tests may patch it
        return RELEASES

    from ...api import log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import github_gateway, github_gateway_for_connection  # noqa: PLC0415 - api imports this module
    account_master_paused = deps.account_master_paused
    bot_identity = deps.bot_identity
    fenced = deps.fenced
    node_auth = deps.node_auth
    quota_peer_release_candidates = deps.quota_peer_release_candidates
    # --- configured Jira version releases ------------------------------------------------------------------
    @app.get("/v1/version-releases/candidates")
    def version_release_candidates(account: str, node: str = Depends(node_auth)) -> dict:
        if blocked := load_policy_api.blocked_offer(ctx, node, "versions"):
            return blocked
        try:
            state = licensing.enforce_work_entitlement(ctx, account, node,
                                                        public_key=ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            return {"versions": [], "reason": str(exc)}
        policy = rollout.node_compatible(ctx, node, _releases())
        if not policy["compatible"]:
            return {"versions": [], "reason": f"version skew: {policy['reason']}"}
        repo = ctx.repo()
        node_row = ctx.db.one("SELECT accepting_work,readiness,tools FROM nodes WHERE name=%s AND removed_at IS NULL", (node,))
        if not node_row or not node_row["accepting_work"]:
            return {"versions": [], "reason": "machine is not accepting new work"}
        if account_master_paused(account):
            return {"versions": [], "reason": "account is paused by master monitoring"}
        if ctx.leases.cooling(account):
            return {"versions": [], "reason": "account quota is cooling or predicted to run out before reset"}
        versions = version_release.candidates(ctx, account, node)
        refusal = system_check_api.release_candidate_refusal(
            node_row.get("readiness"), account, [item.id for item in repo.accounts_on(node)], versions)
        if refusal:
            return refusal
        versions = [item for item in versions
                    if not (item.get("project") in repo.projects
                            and repo.projects[item["project"]].settings_for_repo(item.get("repository", "")).get("containers")
                            and toolchain_gaps(node_row.get("tools") or {}, [CONTAINER_RUNTIME_TOOL], repo.versions))]
        peer_candidates = quota_peer_release_candidates(account)
        available = [candidate for candidate in versions
                     if (candidate["project"], str(candidate["version_id"]),
                         str(candidate.get("repository") or "")) not in peer_candidates]
        if versions and not available:
            return {"versions": [], "reason": "account quota is cooling or predicted to run out before reset"}
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

    @app.post("/v1/version-releases/claim")
    def version_release_claim(body: VersionReleaseClaimBody, node: str = Depends(node_auth)) -> Any:
        load_capacity, state = claim_steps.check_claim_policy(
            ctx, body, node, load_policy_api.version_claim_capacity, licensing.enforce_work_entitlement,
            licensing.LicenseError, rollout.node_compatible, _releases, HTTPException)
        repo, project, acc = claim_steps.check_claim_owner(
            ctx, body, node, state, account_master_paused, licensing.enforce_project_entitlement,
            licensing.LicenseError, shared_accounts.may_take, version_release.enabled, HTTPException)
        claim_steps.check_claim_node(ctx, repo, project, body, node,
                                     system_check_api.require_project_ready, HTTPException)
        project, version = claim_steps.resolve_claim_version(
            ctx, body, project, version_release.project_versions, version_release.project_for_version,
            version_release.enabled, HTTPException)
        claim_steps.check_claim_runtime(
            ctx, repo, project, version, node, body.account, toolchain_gaps, CONTAINER_RUNTIME_TOOL,
            quota_peer_release_candidates, HTTPException)
        claim_github, snapshot = claim_steps.evaluate_claim_gate(
            ctx, project, version, github_gateway, replace, version_release.post_merge_resume,
            version_release.evaluate, version_release.record_gate, HTTPException)
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
            source_branch, source_sha, release_branch, branch_created = claim_steps.create_claim_branch(
                project, version, snapshot, github, version_release.source_branch, version_release.release_branch)
            tracker = version_release.tracker_for(ctx, project, version["id"], version["name"])
            selected_settings = {"repo": project.repo, "base": project.base,
                                 "checks": version_release.check_commands(project), "stack": project.stack,
                                 "containers": bool(project.settings_for_repo(project.repo).get("containers"))}
            claim_values = (run_id, key, project.key, "version_release", acc.id, acc.provider, "", node, lease.fence, "")
            if not load_policy_api.claim_run(ctx, acc, claim_values, load_capacity):
                raise HTTPException(409, f"account {acc.id} has no shared session capacity on {node}")
            claim_steps.persist_claim_rows(ctx, project, version, run_id, tracker, selected_settings, connection_id,
                                           snapshot, J)
            version_release._audit(ctx, project, tracker,
                                   {"id": run_id, "fence": lease.fence, "account_id": acc.id,
                                    "github_connection": connection_id},
                                   "github-release-branch", "created" if branch_created else "existing",
                                   {"branch": release_branch, "source": source_branch, "source_sha": source_sha})
        except Exception:
            ctx.leases.release(key, run_id, lease.fence)
            raise
        ctx.db.run("UPDATE accounts SET status='working',updated_at=now(),idle_since=NULL,cooldown_since=NULL WHERE id=%s",
                   (acc.id,))
        ctx.events.emit("version_release.claimed", ticket=tracker, project=project.key, step="version_release",
                        run_id=run_id, fence=lease.fence, account=acc.id, provider=acc.provider, node=node,
                        outcome="checking", reason=version["name"], data={"version_id": version["id"],
                                                                            "tickets": snapshot["tickets"]})
        project_data = {"key": project.key, "repo": project.repo, "base": project.base,
                        "checks": version_release.check_commands(project), "source_branch": version_release.source_branch(project),
                        "target_branch": version_release.target_branch(project),
                        "release_branch": release_branch, "source_sha": source_sha,
                        "environment": ([item.public() for item in project.environment_items
                                         if "post_merge" in item.runs]),
                        "containers": bool(project.settings_for_repo(project.repo).get("containers")),
                        "work_minutes": project.work_minutes()}
        ticket = {"key": key, "project": project.key, "summary": f"Release {version['name']}",
                  "description": f"Run full configured project checks on {release_branch}, cut from {source_branch}.",
                  "labels": [], "priority": "Highest", "type": "Release"}
        try:
            context_pack = knowledge.context_pack(ctx.db, project=project.key, ticket=key,
                                                  query=f"{ticket['summary']}\n{ticket['description']}", run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - a local search failure cannot prevent a lease
            log.warning("knowledge context pack for release %s failed: %s", version["name"], type(exc).__name__)
            context_pack = {"project": project.key, "ticket": key, "items": []}
        return {"claimed": True, "step": "version_release", "run_id": run_id, "fence": lease.fence,
                "run_token": secrets.token_urlsafe(32), "lease_ms": ctx.settings.lease_ms,
                "provider": acc.provider, "model": "", "effort": "", "account": acc.id,
                "identity": bot_identity(), "ticket": ticket, "project": project_data, "context_pack": context_pack,
                "version": version, "tracker_key": tracker, "tickets": snapshot["tickets"]}

    @app.post("/v1/version-releases/{version_id}/complete")
    def version_release_complete(version_id: str, body: VersionReleaseCompleteBody,
                                 node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "version_release", "complete",
                     {"version_id": version_id, "head_sha": body.head_sha, "checks": body.checks})
        if run["step"] != "version_release" or run["node"] != node:
            raise HTTPException(403, "run is not a version release owned by this node")
        if run["ticket_key"] != version_release.version_identity(run["project"], version_id):
            raise HTTPException(403, "run holds the lease of another Jira version")
        project = ctx.repo().projects.get(run["project"])
        if not project or not version_release.enabled(project):
            raise HTTPException(403, "project is not enabled for version releases")
        version = next((v for v in version_release.project_versions(ctx, project) if v["id"] == version_id), None)
        if not version:
            raise HTTPException(409, "Jira version is no longer configured and unreleased")
        saved_repository = str(((run.get("details") or {}).get("repository") or ""))
        if saved_repository:
            version = {**version, "repository": saved_repository}
        project = version_release.project_for_version(ctx, project, version)
        if project.repo.lower().rstrip("/") == "jooservices/ai-flow":
            raise HTTPException(403, "AI Flow itself is never released by project version releases")
        commands = version_release.check_commands(project)
        reported = [str(item.get("command") or "") for item in body.checks]
        if not commands or any(command not in reported for command in commands):
            body.error = body.error or "Worker did not report every configured full project check."
        report = {"head_sha": body.head_sha, "source_sha": body.source_sha or body.head_sha,
                  "checks": body.checks, "error": body.error}
        connection_id = str(((run.get("details") or {}).get("github_connection") or ""))
        try:
            if connection_id:
                selected_github = github_gateway_for_connection(ctx, connection_id, project.repo)
            else:
                selected_github = github_gateway(ctx, project.repo)
                connection_id = github_connection_used.get()
        except HTTPException:
            raise
        if selected_github is None:
            raise HTTPException(503, "GitHub is not connected")
        run = dict(run)
        if connection_id:
            run["github_connection"] = connection_id
        release_ctx = replace(ctx, github=selected_github)
        try:
            result = version_release.complete(release_ctx, project, version, run, report)
        except Exception as exc:  # noqa: BLE001 - park the version behind one blocker; idempotent once it is Done
            log.exception("version release publication failed for %s %s", project.key, version["name"])
            return version_release.publish_failed(release_ctx, project, version, run, exc)
        ctx.db.run("UPDATE runs SET head_sha=%s,summary=%s,details=details || %s WHERE id=%s",
                   (body.head_sha, f"version release {result['status']}", J({"release_result": result,
                                                                              "checks": body.checks}),
                    body.run_id))
        return result

    @app.post("/v1/environment/post-merge/complete")
    def environment_post_merge_complete(body: PostMergeCompleteBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "environment", "post_merge_complete",
                     {"checks": len(body.checks), "head_sha": body.head_sha[:12]})
        if run["step"] != "post_merge" or run["node"] != node:
            raise HTTPException(403, "only the active post-merge run on this node can record these results")
        row = ctx.db.one("SELECT criteria FROM environment_post_merge_checks WHERE ticket_key=%s"
                         " AND project=%s AND status='pending'", (run["ticket_key"], run["project"]))
        if not row:
            raise HTTPException(409, "ticket has no pending post-merge criteria")
        allowed_checks = {str(item.get("check") or "") for item in row["criteria"] or []
                          if isinstance(item, dict) and item.get("check")}
        if any(str(item.get("command") or "") not in allowed_checks for item in body.checks):
            raise HTTPException(422, "post-merge result contains a check not mapped to this ticket")
        report = {"head_sha": body.head_sha, "checks": body.checks, "error": body.error}
        results = environment_reports.record_post_merge_results(ctx, run, report)
        if not results:
            raise HTTPException(409, "post-merge result was already recorded")
        ctx.db.run("UPDATE runs SET head_sha=%s,details=details || %s WHERE id=%s",
                   (body.head_sha, J({"post_merge_results": results}), body.run_id))
        return {"recorded": True, "results": results}

    return SimpleNamespace(version_release_candidates=version_release_candidates, version_release_claim=version_release_claim, version_release_complete=version_release_complete, environment_post_merge_complete=environment_post_merge_complete)
