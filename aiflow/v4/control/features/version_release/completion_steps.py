"""The steps of completing a version release run and of recording the post-merge results."""
from dataclasses import replace

from fastapi import HTTPException

from ... import environment as environment_reports
from ... import version_release
from ...db import J
from ...route_deps import github_connection_used


def release_run_and_version(ctx, run: dict, node: str, version_id: str):
    """Check that the fenced run is this node's release run of this Jira version; returns (project, version)."""
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
    return project, version


def completion_report(project, body) -> dict:
    commands = version_release.check_commands(project)
    reported = [str(item.get("command") or "") for item in body.checks]
    if not commands or any(command not in reported for command in commands):
        body.error = body.error or "Worker did not report every configured full project check."
    return {"head_sha": body.head_sha, "source_sha": body.source_sha or body.head_sha,
            "checks": body.checks, "error": body.error}


def release_gateway(ctx, run: dict, project, github_gateway, github_gateway_for_connection):
    """(gateway, connection id) the run used, or the repository's current connection."""
    connection_id = str(((run.get("details") or {}).get("github_connection") or ""))
    if connection_id:
        selected_github = github_gateway_for_connection(ctx, connection_id, project.repo)
    else:
        selected_github = github_gateway(ctx, project.repo)
        connection_id = github_connection_used.get()
    if selected_github is None:
        raise HTTPException(503, "GitHub is not connected")
    return selected_github, connection_id


def complete_release(ctx, log, run: dict, project, version, report: dict, body, selected_github, connection_id):
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


def record_post_merge(ctx, run: dict, node: str, body) -> dict:
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
