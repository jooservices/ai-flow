"""Ordered installer groups for project routes.

Route decorators stay with their handlers to preserve the published route contract.
"""
from types import SimpleNamespace
from . import routes as compat


def _install_environment_scan_report_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.get("/v1/environment/scan")
    def environment_scan_report(request: compat.Request) -> dict:
        console_user(request, {"projects.manage"}, target="aiflow-control", action="read")
        return {"scan": compat.environment_reports.last_scan(ctx)}
    return environment_scan_report

def _install_environment_scan_run_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.post("/v1/environment/scan")
    def environment_scan_run(request: compat.Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="environment_scan")
        report = compat.environment_reports.scan(ctx, actor=user.subject)
        compat.publish_for(ctx, compat.ProjectEnvironmentScanned(project="", actor=user.subject,
                                                   result=f"{len(report['offenders'])} of {report['scanned']} open tickets",
                                                   details={"scan": report["id"]}))
        return {"scan": report}
    return environment_scan_run

def _install_environment_removals_route(app, ctx, deps, runtime, support):
    fenced = runtime.fenced
    node_auth = runtime.node_auth
    @app.post("/v1/environment/removals")
    def environment_removals(body: compat.EnvironmentRemovalBody, node: str = compat.Depends(node_auth)) -> dict:
        """The intake AI's record of the wording it removed and the environment needs behind it (fenced)."""
        run = fenced(body.run_id, body.fence, "environment", "removals", {"removed": len(body.removed)})
        if run["step"] != "intake":
            raise compat.HTTPException(403, "only an intake run records removed wording")
        compat.environment_reports.record_removal(ctx, run, body.removed, body.needs)
        tickets = compat.environment_reports.open_need_tickets(ctx, run, body.needs)
        compat.publish_for(ctx, compat.EnvironmentRemovalsRecorded(run_id=body.run_id, fence=body.fence,
                                                     ticket=run["ticket_key"], actor=run["account_id"],
                                                     removed=len(body.removed), tickets=len(tickets),
                                                     needs=len(body.needs)))
        return {"ok": True, "environment_tickets": tickets}
    return environment_removals


def install_environment_routes(app, ctx, deps, runtime, support) -> SimpleNamespace:
    environment_scan_report = _install_environment_scan_report_route(app, ctx, deps, runtime, support)
    environment_scan_run = _install_environment_scan_run_route(app, ctx, deps, runtime, support)
    environment_removals = _install_environment_removals_route(app, ctx, deps, runtime, support)
    return SimpleNamespace(
        environment_scan_report=environment_scan_report,
        environment_scan_run=environment_scan_run,
        environment_removals=environment_removals,
    )
