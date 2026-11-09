"""Route installer for the System check report, its manual run and the support bundle."""
from fastapi import Request
from fastapi.responses import JSONResponse

from ... import system_check


def _visible_project_keys(ctx, request: Request, console_user, optional_console_user):
    """The project keys the caller may see; None means every project."""
    user = (console_user(request, "node.operations.read") if ctx.settings.console_login
            else optional_console_user(request))
    return user.project_keys if user else None


def _register_system_check_report(app, ctx, deps):
    console_user = deps.console_user
    optional_console_user = deps.optional_console_user

    @app.get("/v1/system-check")
    def system_check_report(request: Request) -> dict:
        project_keys = _visible_project_keys(ctx, request, console_user, optional_console_user)
        return {"nodes": system_check.report_nodes(ctx, project_keys)}
    return system_check_report


def _register_system_check_run(app, ctx, deps):
    console_user = deps.console_user

    @app.post("/v1/system-check/run")
    def system_check_run(request: Request) -> dict:
        console_user(request, "nodes.manage", target="system-check", action="run")
        return {"nodes": system_check.refresh_all(ctx, "manual")}
    return system_check_run


def _register_support_bundle(app, ctx, deps):
    console_user = deps.console_user
    optional_console_user = deps.optional_console_user

    @app.get("/v1/support-bundle")
    def support_bundle(request: Request) -> JSONResponse:
        project_keys = _visible_project_keys(ctx, request, console_user, optional_console_user)
        return JSONResponse(system_check.support_bundle(ctx, project_keys), headers={
            "Content-Disposition": 'attachment; filename="aiflow4-support.json"',
            "Cache-Control": "no-store",
        })
    return support_bundle


def install_system_check_routes(app, ctx, deps):
    _register_system_check_report(app, ctx, deps)
    _register_system_check_run(app, ctx, deps)
    _register_support_bundle(app, ctx, deps)
