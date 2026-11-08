"""Ordered installer groups for project routes.

Route decorators stay with their handlers to preserve the published route contract.
"""
from types import SimpleNamespace
from . import routes as compat
from ....core import pipeline_checks


def _install_managed_projects_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    optional_console_user = runtime.optional_console_user
    @app.get("/v1/projects")
    def managed_projects(request: compat.Request) -> dict:
        user = (console_user(request, "projects.manage") if ctx.settings.console_login
                else optional_console_user(request))
        repo = ctx.repo()
        readiness_by_project = compat.system_check_api.projects_readiness(ctx, repo)
        counts = {row["project"]: int(row["count"]) for row in
                  ctx.db.all("SELECT project,count(*) AS count FROM tickets GROUP BY project")}
        probes = compat.tooling.probe_report(ctx)
        projects = []
        for project in sorted(repo.projects.values(), key=lambda item: item.key):
            local_checks, pipeline_only = pipeline_checks.project_catalog(project.local_gate)
            projects.append({"key": project.key, "repo": project.repo,
                             "repositories": project.configured_repositories,
                             "status": "paused" if project.paused else "active",
                             "priority": project.priority, "ticket_count": counts.get(project.key, 0),
                             "base": project.base, "stack": project.stack,
                             "node_readiness": readiness_by_project.get(project.key, []),
                             "services": project.required_services,
                             "checks": probes.get(project.key, {}), "check_commands": list(project.checks),
                             "pipeline_checks": local_checks, "pipeline_only": pipeline_only})
        if user and user.project_keys is not None:
            projects = [project for project in projects if project["key"] in user.project_keys]
        invalid_projects = [{"file": f"{name}.yaml", "reason": reason}
                            for name, reason in sorted(repo.invalid_projects.items())
                            if not user or user.project_keys is None or name in user.project_keys]
        return {"projects": projects, "invalid_projects": invalid_projects}
    return managed_projects

def _install_project_catalog_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.get("/v1/projects/catalog")
    def project_catalog(request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_catalog")
        if not ctx.jira or not runtime.github_connection_list(ctx):
            raise compat.HTTPException(503, "Jira and GitHub must be connected before adding a project")
        try:
            jira_projects = ctx.jira.projects()
            github_repos = compat._github_catalog(runtime, user.subject, "project_catalog")
        except Exception as exc:  # noqa: BLE001 - provider, network, and catalog audit failures map to HTTP 502
            runtime.log.warning("project catalog could not be loaded (%s)", type(exc).__name__)
            raise compat.HTTPException(502, "could not load connected Jira projects and GitHub repositories") from exc
        return {"jira_sites": [compat._jira_site(runtime)], "jira_projects": jira_projects,
                "github_repos": github_repos, "connections": compat._connection_summaries(runtime),
                "stacks": sorted(compat.STACK_TOOLS), "required_statuses": support.required_project_statuses}
    return project_catalog


def install_project_listing_routes(app, ctx, deps, runtime, support) -> SimpleNamespace:
    managed_projects = _install_managed_projects_route(app, ctx, deps, runtime, support)
    project_catalog = _install_project_catalog_route(app, ctx, deps, runtime, support)
    return SimpleNamespace(
        managed_projects=managed_projects,
        project_catalog=project_catalog,
    )
