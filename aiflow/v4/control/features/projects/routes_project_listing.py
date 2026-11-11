"""Ordered installer groups for project routes.

Route decorators stay with their handlers to preserve the published route contract.
"""
from types import SimpleNamespace
from . import routes as compat

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
    project_catalog = _install_project_catalog_route(app, ctx, deps, runtime, support)
    return SimpleNamespace(project_catalog=project_catalog)
