"""Ordered installer groups for project routes.

Route decorators stay with their handlers to preserve the published route contract.
"""
from types import SimpleNamespace
from . import routes as compat


def _install_project_environment_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.get("/v1/projects/{key}/environment")
    def project_environment(key: str, request: compat.Request) -> dict:
        console_user(request, {"projects.manage"}, target="aiflow-control", action="read", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            raise compat.HTTPException(404, "project is not configured")
        items, problems = compat.parse_env_items(project.environment)
        return {"project": key, "items": [item.public() for item in items], "problems": problems,
                "kinds": list(compat.ENV_KINDS), "runs": list(compat.ENV_RUN_STEPS),
                "missing": [r for r in compat.environment_reports.reports(ctx) if r["project"] == key],
                "post_merge": compat.environment_reports.post_merge_report(ctx, key)}
    return project_environment

def _install_save_project_environment_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.put("/v1/projects/{key}/environment")
    def save_project_environment(key: str, body: compat.ProjectEnvironmentBody, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="save_project_environment", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            compat._project_audit(runtime, "save_project_environment", key[:32], user.subject, False, "project is not configured")
            raise compat.HTTPException(404, "project is not configured")
        items, problems = compat.parse_env_items(body.items)
        if compat._project_environment_is_invalid(problems, items, body.items):
            message, details = compat._project_environment_error(key, problems, body.items)
            compat._project_audit(runtime, "save_project_environment", key, user.subject, False, message[:300], details)
            raise compat.HTTPException(422, message)
        config_path = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            config = compat.yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            content = compat._project_environment_content(config, items)
            control_name = compat.Path(ctx.settings.control_repo).name
            github = runtime.github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                       f"projects: update the environment of {key} via AI Flow console")
        except compat.HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - the GitHub App call fails in many ways (API, transport, config); all map to 502
            runtime.log.warning("project environment update failed (%s)", type(exc).__name__)
            compat._project_audit(runtime, "save_project_environment", key, user.subject, False, "control repo commit failed")
            raise compat.HTTPException(502, "could not save the environment to aiflow-control") from exc
        compat._refresh_projects(runtime)
        # names only in the audit: the values are configuration, the secrets are never in the file
        compat.publish_for(ctx, compat.ProjectEnvironmentSaved(project=key, actor=user.subject,
                                                 result=commit or "bot commit created",
                                                 details={"project": key, "names": [i.name for i in items]}))
        ctx.events.emit("project.environment_changed", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}", data={"items": len(items)})
        return {"saved": True, "project": key, "commit": commit, "items": [i.public() for i in items]}
    return save_project_environment


def install_project_environment_routes(app, ctx, deps, runtime, support) -> SimpleNamespace:
    project_environment = _install_project_environment_route(app, ctx, deps, runtime, support)
    save_project_environment = _install_save_project_environment_route(app, ctx, deps, runtime, support)
    return SimpleNamespace(
        project_environment=project_environment,
        save_project_environment=save_project_environment,
    )
