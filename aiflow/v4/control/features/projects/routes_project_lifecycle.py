"""Ordered installer groups for project routes.

Route decorators stay with their handlers to preserve the published route contract.
"""
from types import SimpleNamespace
from . import routes as compat


def _install_project_preflight_endpoint_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.post("/v1/projects/preflight")
    def project_preflight_endpoint(body: compat.ProjectPreflightBody, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_preflight")
        return compat._project_preflight(runtime, body, user.subject)
    return project_preflight_endpoint

def _install_add_project_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    require_capacity = runtime.require_capacity
    @app.post("/v1/projects")
    def add_project(body: runtime.project_create_body, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="add_project",
                            node=body.project[:32])
        unknown = compat._unknown_project_services(body.services)
        if unknown:
            raise compat.HTTPException(422, "unknown test services: " + ", ".join(unknown) + ". Use postgres or redis.")
        with ctx.db.pool.connection() as connection:
            with connection.transaction():
                # Keep the shared admission lock through preflight, the external commit, and repo refresh. A
                # competing request then counts the project just committed before it can take the final slot.
                compat.licensing.lock_resource_admission(connection)
                if not compat._refresh_projects(runtime):
                    compat._project_audit(runtime, "add_project", body.project[:32], user.subject, False,
                                  "control repo refresh failed before capacity check", {"repo": body.repo})
                    raise compat.HTTPException(503, "could not refresh the control repo before checking project capacity")
                require_capacity("projects", len(ctx.repo().projects), connection=connection)
                try:
                    result = compat._project_preflight(runtime, body, user.subject)
                except compat.HTTPException as exc:
                    compat._project_audit(runtime, "add_project", body.project[:32], user.subject, False, str(exc.detail),
                                  {"project_input": body.project, "repo": body.repo})
                    raise
                key = result["project"]
                if key in ctx.repo().projects:
                    compat._project_audit(runtime, "add_project", key, user.subject, False, "project is already configured",
                                  {"repo": body.repo})
                    raise compat.HTTPException(409, "project is already configured")
                if not result["ok"]:
                    reasons, detail = compat._project_preflight_failure(body, result)
                    compat._project_audit(runtime, "add_project", key, user.subject, False, "; ".join(reasons), detail)
                    raise compat.HTTPException(422, detail)

                worker_nodes = {row["name"] for row in ctx.db.all(
                    "SELECT name FROM nodes WHERE role='worker' AND removed_at IS NULL")}
                config = compat._new_project_config(body, key)
                content = compat.yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
                compat.inventory.assign_project(connection, key, sorted(worker_nodes))
                control_name = compat.Path(ctx.settings.control_repo).name
                commit = compat._commit_new_project(runtime, ctx, control_name, key, user.subject, content, config)
                # Make the committed project visible to the next admission before releasing the lock.
                compat._refresh_projects(runtime)
        compat.publish_for(ctx, compat.ProjectAdded(project=key, actor=user.subject,
                                      result=commit or "bot commit created", details=config))
        ctx.events.emit("project.added", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}")
        compat.system_check.refresh_all(ctx, "project_added")
        return {"saved": True, "project": key, "commit": commit, "preflight": result}
    return add_project

def _install_save_project_services_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.post("/v1/projects/{key}/services")
    def save_project_services(key: str, body: compat.ProjectServicesBody, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_services", node=key)
        if not compat.re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            compat._project_audit(runtime, "project_services", key[:32], user.subject, False, "project is not configured")
            raise compat.HTTPException(404, "project is not configured")
        unknown = [s for s in body.services if s.strip() and not compat.normalize_services(s)]
        if unknown:
            raise compat.HTTPException(422, "unknown test services: " + ", ".join(unknown) + ". Use postgres or redis.")
        try:
            names, commit = runtime.declare_project_services(ctx, key, body.services, f"projects: test services of {key} via AI Flow console")
        except compat.HTTPException:
            raise
        except Exception as exc:
            runtime.log.warning("project services update failed (%s)", type(exc).__name__)
            compat._project_audit(runtime, "project_services", key, user.subject, False, "control repo commit failed", {"services": body.services})
            raise compat.HTTPException(502, "could not update the project in aiflow-control") from exc
        compat.publish_for(ctx, compat.ProjectServicesChanged(project=key, actor=user.subject,
                                                result=commit or "bot commit created", details={"services": names}))
        ctx.events.emit("project.services_saved", project=key, account=user.subject, outcome="saved",
                        reason=", ".join(names) or "none")
        return {"saved": True, "project": key, "services": names, "commit": commit}
    return save_project_services

def _install_pause_project_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.post("/v1/projects/{key}/pause")
    def pause_project(key: str, body: compat.ProjectPauseBody, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="pause_project", node=key)
        if not compat.re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            compat._project_audit(runtime, "pause_project", key[:32], user.subject, False, "project is not configured")
            raise compat.HTTPException(404, "project is not configured")
        project_file = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            config = compat.yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
            config["paused"] = body.paused
            content = compat.yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
            control_name = compat.Path(ctx.settings.control_repo).name
            github = runtime.github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                            f"projects: {'pause' if body.paused else 'resume'} {key}")
        except compat.HTTPException:
            raise
        except Exception as exc:
            runtime.log.warning("project pause update failed (%s)", type(exc).__name__)
            compat._project_audit(runtime, "pause_project", key, user.subject, False, "control repo commit failed",
                          {"paused": body.paused})
            raise compat.HTTPException(502, "could not update the project in aiflow-control") from exc
        compat._refresh_projects(runtime)
        action = "paused" if body.paused else "resumed"
        compat.publish_for(ctx, compat.ProjectPauseChanged(project=key, actor=user.subject,
                                             result=commit or f"{action} by bot commit",
                                             details={"project": key, "paused": body.paused}))
        ctx.events.emit(f"project.{action}", project=key, account=user.subject, outcome=action,
                        reason=f"control repo commit {commit or 'created'}")
        return {"project": key, "status": "paused" if body.paused else "active", "commit": commit}
    return pause_project

def _install_remove_project_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.delete("/v1/projects/{key}")
    def remove_project(key: str, request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="remove_project", node=key)
        if not compat.re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            compat._project_audit(runtime, "remove_project", key[:32], user.subject, False, "project is not configured")
            raise compat.HTTPException(404, "project is not configured")
        control_name = compat.Path(ctx.settings.control_repo).name
        try:
            github = runtime.github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.delete_file(control_name, branch, f"projects/{key}.yaml",
                                            f"projects: remove {key} via AI Flow console")
        except compat.HTTPException:
            raise
        except Exception as exc:
            runtime.log.warning("project remove commit failed (%s)", type(exc).__name__)
            compat._project_audit(runtime, "remove_project", key, user.subject, False, "control repo commit failed")
            raise compat.HTTPException(502, "could not remove the project from aiflow-control") from exc
        compat._refresh_projects(runtime)
        compat.publish_for(ctx, compat.ProjectRemoved(project=key, actor=user.subject,
                                        result=commit or "bot commit created", details={"project": key}))
        ctx.events.emit("project.removed", project=key, account=user.subject, outcome="removed",
                        reason=f"control repo commit {commit or 'created'}")
        return {"removed": True, "project": key, "commit": commit}
    return remove_project


def install_project_lifecycle_routes(app, ctx, deps, runtime, support) -> SimpleNamespace:
    project_preflight_endpoint = _install_project_preflight_endpoint_route(app, ctx, deps, runtime, support)
    add_project = _install_add_project_route(app, ctx, deps, runtime, support)
    save_project_services = _install_save_project_services_route(app, ctx, deps, runtime, support)
    pause_project = _install_pause_project_route(app, ctx, deps, runtime, support)
    remove_project = _install_remove_project_route(app, ctx, deps, runtime, support)
    return SimpleNamespace(
        project_preflight_endpoint=project_preflight_endpoint,
        add_project=add_project,
        save_project_services=save_project_services,
        pause_project=pause_project,
        remove_project=remove_project,
    )
