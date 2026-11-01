"""Console gate: bearer route detection, role permissions, project scope, refusals, and browser redirects."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from .application_context import ApplicationContext
from . import setup_wizard
from .console_auth import (COOKIE_NAME, PAGE_VIEWS, ConsoleUser, has_permission, landing_page,
                           permissions_for_view, resolve_session_user, verify_session)
from .login import OPEN_PATHS, OPEN_PREFIXES, PAGES, safe_next
from .route_registry import concrete_routes

EXACT_API_PERMISSIONS = {
    "/v1/system-check": "node.operations.read",
    "/v1/support-bundle": "node.operations.read",
    "/v1/role-health": "view.machines_ai",
    "/v1/master/autonomy": "view.machines_ai",
    "/v1/master/report": "view.machines_ai",
    "/v1/provider-routing": "view.machines_ai",
    "/v1/role-health/settings": "console.manage",
    "/v1/master/autonomy/budget": "console.manage",
    "/v1/provider-routing/settings": "console.manage",
    "/v1/reviews": "view.human_review",
    "/v1/logs": "view.logs",
    "/v1/state": "view.dashboard",
}
PREFIX_API_PERMISSIONS = (
    ("/v1/provider-routing/decisions", "view.machines_ai"),
    ("/v1/console/users", "console.manage"),
    ("/v1/console/audit", "console.manage"),
    ("/v1/console/view-as", "console.manage"),
    ("/v1/runtime-settings", "console.manage"),
    ("/v1/quota-refresh/", "console.manage"),
    ("/v1/knowledge/", "view.knowledge"),
    ("/v1/integrations/", "view.integrations"),
    ("/v1/reviews/", "view.human_review"),
    ("/v1/business/", "view.business"),
    ("/v1/stakeholder/", "view.stakeholder"),
    ("/v1/nodes/", "view.machines_ai"),
    ("/v1/accounts/", "view.machines_ai"),
    ("/v1/services", "view.machines_ai"),
    ("/v1/sa/", "view.machines_ai"),
    ("/v1/log-sources", "view.logs"),
    ("/v1/logs/", "view.logs"),
    ("/v1/memory/", "view.knowledge"),
    ("/v1/tickets/", "view.work_board"),
    ("/v1/analytics/", "view.dashboard"),
)


@dataclass(frozen=True)
class GateParts:
    app: FastAPI
    ctx: ApplicationContext
    settings: object
    token_routes: dict[int, bool] = field(default_factory=dict)


def api_permission(path: str) -> str:
    """Return the view permission that owns a console API path."""
    exact = EXACT_API_PERMISSIONS.get(path)
    if exact:
        return exact
    if path.startswith("/v1/runs/") and path.endswith("/inbox"):
        return "review.actions"
    for prefix, permission in PREFIX_API_PERMISSIONS:
        if path.startswith(prefix):
            return permission
    if path.startswith("/v1/projects"):
        if "/study-cases" in path:
            return "view.knowledge"
        if any(suffix in path for suffix in ("/versions", "/report", "/close")):
            return "view.delivery"
        return "projects.manage"
    return "view.dashboard"


def project_for_path(ctx, request: Request) -> str:
    path = request.url.path
    match = re.match(r"^/v1/(?:business|stakeholder|projects)/([^/]+)", path)
    if match:
        return match.group(1)
    match = re.match(r"^/v1/tickets/([^/]+)", path)
    if match:
        row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (match.group(1),))
        return str((row or {}).get("project") or "")
    match = re.match(r"^/v1/reviews/([^/]+)", path)
    if match:
        row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (match.group(1),))
        return str((row or {}).get("project") or "")
    if path.startswith(("/v1/analytics/", "/v1/memory/")) or path in ("/v1/reviews", "/v1/reviews/export.csv"):
        return request.query_params.get("project", "")
    return ""


def login_redirect_target(path: str, query: str = "") -> str:
    target = safe_next(path + (f"?{query}" if query else ""))
    return "/login?next=" + quote(target, safe="")


def flatten_dependencies(dependant):
    yield dependant
    for sub in dependant.dependencies:
        yield from flatten_dependencies(sub)


def route_checks_token(route: APIRoute) -> bool:
    if getattr(route.endpoint, "checks_bearer", False):
        return True
    return any(param.name == "authorization" for dependency in flatten_dependencies(route.dependant)
               for param in dependency.header_params)


def resolved_route(route, scope):
    """Return a matching leaf route when FastAPI wraps included routers in the app route table."""
    if isinstance(route, APIRoute):
        return route
    matcher = getattr(route, "_match", None)
    if callable(matcher):
        result = matcher(scope)
        if result[0] == Match.FULL and len(result) > 2:
            return resolved_route(result[2], scope)
    return None


def checks_token(app: FastAPI, token_routes: dict[int, bool], request: Request) -> bool:
    for route in concrete_routes(app.routes):
        if route.matches(request.scope)[0] == Match.FULL:
            matched = resolved_route(route, request.scope)
            if matched is None:
                return False
            if id(matched) not in token_routes:
                token_routes[id(matched)] = route_checks_token(matched)
            return token_routes[id(matched)]
    return False


def audit_refusal(ctx, user: ConsoleUser, request: Request, reason: str = "not available for your role") -> None:
    from .db import J

    ctx.db.run("INSERT INTO write_audit (target,action,ticket,run_id,fence,actor,accepted,result,payload)"
               " VALUES ('console','access_denied','', '', NULL,%s,false,%s,%s)",
               (user.subject, reason, J({"path": request.url.path, "method": request.method,
                                         "roles": list(user.effective_roles)})))


def active_user(ctx, settings, request: Request, allow_pending: bool = False) -> ConsoleUser | None:
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret, None, allow_pending)
    return resolve_session_user(ctx.db, claims, allow_pending) if claims else None


def forbidden_page() -> HTMLResponse:
    return HTMLResponse("<!doctype html><html lang='en'><meta charset='utf-8'><title>View unavailable</title>"
                        "<main style='font:16px system-ui;max-width:42rem;margin:12vh auto;padding:2rem'>"
                        "<h1>View unavailable</h1><p>This view is not available for your role.</p>"
                        "<a href='/cc'>Return to Dashboard</a></main></html>", status_code=403,
                        headers={"Cache-Control": "no-store"})


def is_open_request(ctx, path: str) -> bool:
    setup_path = path == "/setup" or path.startswith("/v1/setup/")
    setup_open = setup_path and setup_wizard.user_count(ctx) == 0
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES) or setup_open


def is_browser_page(path: str) -> bool:
    return path in PAGES or path.startswith("/help/")


async def anonymous_request(parts: GateParts, request: Request, call_next, bearer_route: bool):
    path = request.url.path
    if bearer_route:
        return await call_next(request)
    if await run_in_threadpool(active_user, parts.ctx, parts.settings, request, True):
        if path in PAGES or path.startswith("/v1/security/mfa") or path == "/v1/auth/me":
            return await call_next(request)
        return JSONResponse({"detail": "set up two-factor sign-in first", "mfa_setup_required": True}, status_code=403)
    if is_browser_page(path):
        query = request.url.query
        return RedirectResponse(login_redirect_target(path, query), status_code=302)
    if request.cookies.get(COOKIE_NAME):
        return JSONResponse({"detail": "not signed in"}, status_code=401)
    return JSONResponse({"detail": "sign in required"}, status_code=401)


def permission_refusal(parts: GateParts, user: ConsoleUser, request: Request, page_view) -> Response | None:
    path = request.url.path
    required = permissions_for_view(page_view) if page_view else api_permission(path)
    if path == "/v1/console/view-as/stop":
        allowed = user.is_administrator
    else:
        allowed = has_permission(user.effective_roles, required)
    project = project_for_path(parts.ctx, request)
    if allowed and project and user.project_keys is not None:
        allowed = project in user.project_keys
    if allowed:
        return None
    audit_refusal(parts.ctx, user, request)
    if page_view:
        return forbidden_page()
    return JSONResponse({"detail": "not available for your role"}, status_code=403)


def set_authenticated_request_user(request: Request, user: ConsoleUser, page_view) -> None:
    path = request.url.path
    if page_view is not None:
        request.state.console_user = user
    if path in PAGES and user.view_as:
        request.state.console_user = user
    if path.startswith("/v1/"):
        request.state.console_user = user


def authenticated_bearer_request(request: Request, user: ConsoleUser, bearer_route: bool) -> bool:
    return bool(bearer_route and request.cookies.get(COOKIE_NAME) and user)


async def authenticated_request(parts: GateParts, request: Request, call_next, user: ConsoleUser,
                                bearer_route: bool):
    path = request.url.path
    if path == "/" and not user.view_as:
        return RedirectResponse(landing_page(user.assigned_roles), status_code=302)
    page_view = PAGE_VIEWS.get(path)
    refusal = permission_refusal(parts, user, request, page_view)
    if refusal is not None:
        return refusal
    set_authenticated_request_user(request, user, page_view)
    if authenticated_bearer_request(request, user, bearer_route):
        return await call_next(request)
    if is_browser_page(path):
        _ = safe_next(path + (f"?{request.url.query}" if request.url.query else ""))
        request.state.console_user = user
    return await call_next(request)


async def gate_request(parts: GateParts, request: Request, call_next):
    path = request.url.path
    if is_open_request(parts.ctx, path):
        return await call_next(request)
    bearer_route = bool(request.headers.get("authorization") and
                        checks_token(parts.app, parts.token_routes, request))
    user = await run_in_threadpool(active_user, parts.ctx, parts.settings, request)
    if user is None:
        return await anonymous_request(parts, request, call_next, bearer_route)
    return await authenticated_request(parts, request, call_next, user, bearer_route)


def install_gate(app: FastAPI, ctx: ApplicationContext) -> None:
    parts = GateParts(app, ctx, ctx.settings)

    @app.middleware("http")
    async def gate(request: Request, call_next):
        return await gate_request(parts, request, call_next)
