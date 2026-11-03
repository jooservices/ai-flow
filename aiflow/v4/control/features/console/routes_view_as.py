"""Administrator view-as routes."""
from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ...console_auth import (COOKIE_NAME, ROLE_LABELS, issue_session, normalize_role, resolve_session_user,
                             verify_session)
from ...features.work.constants import utcnow
from . import routes as console_routes
from .bodies import ViewAsBody
from .events import ViewAsStarted, ViewAsStopped


def install_view_as_routes(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user

    @app.post("/v1/console/view-as")
    def console_view_as(body: ViewAsBody, request: Request) -> JSONResponse:
        user = console_user(request, "console.manage", target="console", action="view_as")
        try:
            role = normalize_role(body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        from ...login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret, view_as=role, sid=user.sid)
        console_routes.publish_for(ctx, ViewAsStarted(actor=user.subject, role=role))
        response = JSONResponse({"viewing_as": role, "role_label": ROLE_LABELS[role]})
        app.state.set_session_cookies(response, token, SESSION_TTL_S)  # keeps the session id and the CSRF token in step
        return response

    @app.post("/v1/console/view-as/stop")
    def console_stop_view_as(request: Request) -> JSONResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user or not user.is_administrator:
            raise HTTPException(403, "not available for your role")
        from ...login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret, sid=user.sid)
        console_routes.publish_for(ctx, ViewAsStopped(actor=user.subject, previous_role=user.view_as or ""))
        response = JSONResponse({"viewing_as": None})
        app.state.set_session_cookies(response, token, SESSION_TTL_S)
        return response

    return SimpleNamespace(console_view_as=console_view_as, console_stop_view_as=console_stop_view_as)
