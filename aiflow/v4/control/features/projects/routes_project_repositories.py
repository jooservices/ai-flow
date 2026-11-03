"""Ordered installer groups for project routes.

Route decorators stay with their handlers to preserve the published route contract.
"""
from types import SimpleNamespace
from . import routes as compat


def _install_project_repositories_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.get("/v1/projects/{key}/repositories")
    def project_repositories(key: str, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="project_repositories", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            raise compat.HTTPException(404, "project is not configured")
        if not ctx.jira or not runtime.github_connection_list(ctx):
            raise compat.HTTPException(503, "Jira and GitHub must be connected to configure repositories")
        try:
            epics = compat._jira_project_epics(runtime, key)
            available = compat._github_catalog(runtime, user.subject, "project_repositories")
        except Exception as exc:
            runtime.log.warning("project repositories could not be loaded (%s)", type(exc).__name__)
            raise compat.HTTPException(502, "could not load Jira epics and GitHub repositories") from exc
        by_name = {str(item["full_name"]).lower(): item for item in available}
        assignments = runtime.github_connections(ctx).assignments()
        repositories = []
        for row in compat._repository_settings_payload(project):
            catalog_row = by_name.get(row["repo"].lower()) or {}
            reachable = catalog_row.get("connections", [])
            assigned_connection = assignments.get(row["repo"].lower(), catalog_row.get("assigned_connection", ""))
            stale_assignment = bool(assigned_connection and assigned_connection not in reachable)
            selected_connection = catalog_row.get("connection", "")
            if stale_assignment:
                selected_connection = reachable[0] if reachable else ""
            repositories.append({**row, "connection": selected_connection,
                                 "assigned_connection": assigned_connection,
                                 "connection_stale": stale_assignment,
                                 "connections": reachable})
        return {"project": key, "default_repo": project.repo, "connections": compat._connection_summaries(runtime),
                "project_defaults": {"base": project.base, "checks": project.checks, "stack": project.stack,
                                     "containers": project.containers},
                "repositories": repositories,
                "epic_repositories": project.epic_repositories or {},
                "epics": epics, "github_repos": available, "stacks": sorted(compat.STACK_TOOLS)}
    return project_repositories

def _install_save_project_repositories_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.put("/v1/projects/{key}/repositories")
    def save_project_repositories(key: str, body: compat.ProjectRepositoriesBody, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="save_project_repositories", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            compat._project_audit(runtime, "save_project_repositories", key[:32], user.subject, False, "project is not configured")
            raise compat.HTTPException(404, "project is not configured")
        if not ctx.jira or not runtime.github_connection_list(ctx):
            compat._project_audit(runtime, "save_project_repositories", key, user.subject, False,
                          "Jira and GitHub must be connected")
            raise compat.HTTPException(503, "Jira and GitHub must be connected")
        try:
            available, gateways, epic_keys = compat.load_project_repository_save_context(
                key, user.subject, ctx, support.jira_project_epics, support.github_catalog, runtime.github_connection_list)
        except Exception as exc:
            compat._project_audit(runtime, "save_project_repositories", key, user.subject, False, "could not load providers")
            runtime.log.warning("project repository save preflight failed (%s)", type(exc).__name__)
            raise compat.HTTPException(502, "could not load Jira epics and GitHub repositories") from exc
        repos, chosen, mappings, error = compat.validate_project_repository_save(
            body, key, project, epic_keys, available, gateways)
        if error:
            compat._project_audit(runtime, "save_project_repositories", key, user.subject, False, error,
                          {"epic_repositories": mappings, "repositories": list(repos.values())})
            raise compat.HTTPException(422, error)

        config_path = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            normalized, commit = compat.commit_project_repository_settings(
                ctx, config_path, key, repos, mappings, runtime.github_control_gateway)
        except compat.HTTPException:
            raise
        except Exception as exc:
            runtime.log.warning("project repository update failed (%s)", type(exc).__name__)
            compat._project_audit(runtime, "save_project_repositories", key, user.subject, False, "control repo commit failed",
                          {"epic_repositories": mappings, "repositories": list(repos.values())})
            raise compat.HTTPException(502, "could not save repository settings to aiflow-control") from exc
        for lowered, connection in chosen.items():
            runtime.github_connections(ctx).assign(repos[lowered]["repo"], connection)
            compat.publish_for(ctx, compat.GitHubConnectionAssigned(actor=user.subject, repo=repos[lowered]["repo"],
                                                      connection=connection, project=key))
        compat._refresh_projects(runtime)
        compat.publish_for(ctx, compat.ProjectRepositoriesSaved(project=key, actor=user.subject,
                                                  result=commit or "bot commit created",
                                                  details={"epic_repositories": mappings,
                                                           "repositories": normalized}))
        ctx.events.emit("project.repositories_changed", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}",
                        data={"epic_count": len(mappings), "repository_count": len(normalized)})
        return {"saved": True, "project": key, "commit": commit,
                "epic_repositories": mappings, "repositories": normalized}
    return save_project_repositories


def install_project_repository_routes(app, ctx, deps, runtime, support) -> SimpleNamespace:
    project_repositories = _install_project_repositories_route(app, ctx, deps, runtime, support)
    save_project_repositories = _install_save_project_repositories_route(app, ctx, deps, runtime, support)
    return SimpleNamespace(
        project_repositories=project_repositories,
        save_project_repositories=save_project_repositories,
    )
