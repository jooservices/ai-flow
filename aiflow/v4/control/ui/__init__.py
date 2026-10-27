"""Console views for Dashboard, Work board, Delivery, SA, Machines and AI, Users and roles,
Team Lead, Business, Human review, Stakeholder, Knowledge, Integrations, Projects, Licensing and Backup.

Dashboard (/dashboard, /cc), Work board (/work, /board), Delivery (/delivery), SA health (/sa),
Machines and AI (/machines), Users and roles (/users), Team Lead (/lead),
Business (/business), Human review (/review), Stakeholder (/stakeholder), Knowledge (/knowledge),
Integrations (/integrations, control/jira_connect.py), Projects (/projects), Licensing (/licensing),
Backup (/backup), Notifications (/notifications) and Support (/support), fed by /v1/state and relevant view APIs.
All views are fed by the live registry; the Business view adds
/v1/business/<project>/change-requests (see control/business.py). The console renders the live registry; extra
read-only endpoints give its drawers ticket details, runs, events, audited writes and time segments.
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..console_auth import COOKIE_NAME, resolve_session_user, role_satisfies, verify_session
from ..ticket_detail import TicketNotFound, ticket_detail
from . import assets

HERE = Path(__file__).parent

# Keep the address inventory shared with the login gate and route tests; `/cc` and `/board` are view aliases.
CONSOLE_PAGE_PATHS = (
    "/", "/dashboard", "/work", "/board", "/delivery", "/sa", "/business", "/review", "/stakeholder",
    "/machines", "/users", "/knowledge", "/backup", "/integrations", "/help", "/licensing", "/settings",
    "/notifications", "/lead", "/projects", "/support", "/cc",
)


def mount_ui(app: FastAPI, ctx) -> None:
    page = HERE / "console.html"
    logs_page = HERE / "logs.html"
    assets.register_assets(app)
    _register_console_routes(app, page)
    _register_logs_route(app, ctx, logs_page)
    _register_help_routes(app)
    _register_notifications_route(app, ctx, page)
    _register_settings_route(app, ctx, page)
    _register_ticket_route(app, ctx)
    _register_services_route(app)


def _register_console_routes(app: FastAPI, page: Path) -> None:
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    def console() -> FileResponse:
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    for path in CONSOLE_PAGE_PATHS:
        if path not in {"/", "/help", "/notifications", "/settings", "/logs"}:
            app.add_api_route(path, console, methods=["GET"], include_in_schema=False)


def _register_logs_route(app: FastAPI, ctx, logs_page: Path) -> None:
    @app.get("/logs", include_in_schema=False)
    def logs(request: Request) -> FileResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user:
            raise HTTPException(401, "authenticated console session required")
        if not role_satisfies(user.effective_roles, {"view.logs"}):
            raise HTTPException(403, "not available for your role")
        return FileResponse(logs_page, media_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})


def _register_help_routes(app: FastAPI) -> None:
    @app.get("/help", include_in_schema=False)
    def help_root() -> RedirectResponse:
        return RedirectResponse("/help/")

    @app.get("/help/{path:path}", include_in_schema=False)
    def help_site(path: str = "") -> FileResponse:
        """The documentation site that ships in the release (docs/v4/documentation-site.md); the console Help
        links open /help/screens/<view>.html."""
        from ...core.docsite import site_dir
        root = site_dir()
        if root is None:
            raise HTTPException(404, "the documentation site is not installed")
        root = root.resolve()
        target = (root / (path or "index.html")).resolve()
        if target.is_dir():
            target = target / "index.html"
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, "no such documentation page")
        return FileResponse(target, headers={"Cache-Control": "no-cache"})


def _register_notifications_route(app: FastAPI, ctx, page: Path) -> None:
    @app.get("/notifications", include_in_schema=False)
    def notifications(request: Request) -> FileResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user:
            raise HTTPException(401, "authenticated console session required")
        if not role_satisfies(user.effective_roles, {"notifications.manage"}):
            raise HTTPException(403, "not available for your role")
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})


def _register_settings_route(app: FastAPI, ctx, page: Path) -> None:
    @app.get("/settings", include_in_schema=False)
    def settings_page(request: Request) -> FileResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user:
            raise HTTPException(401, "authenticated console session required")
        if not role_satisfies(user.effective_roles, {"console.manage"}):
            raise HTTPException(403, "not available for your role")
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})


def _register_ticket_route(app: FastAPI, ctx) -> None:
    @app.get("/v1/tickets/{key}")
    def ticket(key: str) -> dict:
        try:
            return ticket_detail(ctx, key)
        except TicketNotFound:
            raise HTTPException(404, "unknown ticket") from None


def _register_services_route(app: FastAPI) -> None:
    @app.get("/v1/services")
    def services() -> dict:
        """Active checks from Gatus (name, group, up, response time)."""
        try:
            data = httpx.get("http://aiflow4-gatus:8080/api/v1/endpoints/statuses", timeout=5).json()
        except Exception:  # noqa: BLE001
            return {"services": [], "error": "gatus unreachable"}
        out = []
        for e in data:
            last = (e.get("results") or [{}])[-1]
            out.append({"name": e.get("name"), "group": e.get("group"), "up": bool(last.get("success")),
                        "ms": int((last.get("duration") or 0) / 1_000_000), "errors": last.get("errors") or []})
        return {"services": out}
