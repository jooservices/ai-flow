"""Licence activation, limits and usage routes."""
from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse

from ... import licensing
from . import routes as console_routes
from .bodies import LicenseKeyBody, OfflineLicenseBody, PlanLimitsBody
from .events import LicenceActivated, LicenceLimitsUpdated


def install_licensing_routes(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user

    @app.get("/v1/licensing")
    def licensing_state(request: Request) -> dict:
        console_user(request, "licensing.manage")
        state = licensing.snapshot(ctx.db, ctx.settings.license_public_key)
        state["usage"] = licensing.resource_counts(ctx)
        state["sync_warnings"] = list(app.state.license_sync_warnings)
        return state

    @app.get("/v1/licensing/status")
    def licensing_status(request: Request) -> dict:
        console_user(request, "console.read")
        return licensing.status_fields(licensing.snapshot(ctx.db, ctx.settings.license_public_key))

    @app.post("/v1/licensing/activate")
    def activate_license(body: LicenseKeyBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            state = licensing.activate_online(ctx.db, body.key, ctx.settings.license_service_url,
                                              ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        console_routes.publish_for(ctx, LicenceActivated(actor=user.subject, source="online", plan=state["plan"]))
        return state

    @app.post("/v1/licensing/offline")
    def activate_license_offline(body: OfflineLicenseBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            state = licensing.activate(ctx.db, body.license_file, ctx.settings.license_public_key, "offline")
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        console_routes.publish_for(ctx, LicenceActivated(actor=user.subject, source="offline", plan=state["plan"]))
        return state

    @app.put("/v1/licensing/limits")
    def update_license_limits(body: PlanLimitsBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_limits")
        try:
            state = licensing.save_limits(ctx.db, body.plan, body.limits, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        console_routes.publish_for(ctx, LicenceLimitsUpdated(actor=user.subject, plan=body.plan, limits=body.limits))
        return state

    @app.get("/v1/licensing/usage")
    def licensing_usage(request: Request) -> dict:
        console_user(request, "console.read")
        return {"rows": licensing.usage_report(ctx.db)}

    @app.get("/v1/licensing/usage.csv")
    def licensing_usage_csv(request: Request) -> PlainTextResponse:
        console_user(request, "console.read")
        content = licensing.usage_csv(licensing.usage_report(ctx.db))
        return PlainTextResponse(content, media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=ai-flow-usage.csv"})

    return SimpleNamespace(licensing_state=licensing_state, licensing_status=licensing_status,
                           activate_license=activate_license, activate_license_offline=activate_license_offline,
                           update_license_limits=update_license_limits, licensing_usage=licensing_usage,
                           licensing_usage_csv=licensing_usage_csv)
