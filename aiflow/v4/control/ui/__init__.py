"""Console views for Dashboard, Work board, Delivery, SA, Machines and AI, Users and roles,
Team Lead, Business, Human review, Stakeholder, Knowledge, Integrations, Projects, System check, Licensing and Backup.

Dashboard (/dashboard, /cc), Work board (/work, /board), Delivery (/delivery), SA health (/sa),
Machines and AI (/machines), Users and roles (/users), Team Lead (/lead),
Business (/business), Human review (/review), Stakeholder (/stakeholder), Knowledge (/knowledge),
Integrations (/integrations, control/jira_connect.py), Projects (/projects), System check (/system-check),
Licensing (/licensing), Backup (/backup), Notifications (/notifications) and Support (/support), fed by /v1/state and relevant view APIs.
All views are fed by the live registry; the Business view adds
/v1/business/<project>/change-requests (see control/business.py). The console renders the live registry; extra
read-only endpoints give its drawers ticket details, runs, events, audited writes and time segments.
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ..console_auth import COOKIE_NAME, resolve_session_user, role_satisfies, verify_session
from ..features.tickets.repository import TicketRepository
from aiflow.v4.control.features.tickets import ticket_repository_of
from .console_build import (
    build_console_page as build_console_page,
    build_served_console_page,
    console_source_names as console_source_names,
    console_template as console_template,
)

HERE = Path(__file__).parent

# Keep the address inventory shared with the login gate and route tests; `/cc` and `/board` are view aliases.
CONSOLE_PAGE_PATHS = (
    "/", "/dashboard", "/work", "/board", "/delivery", "/sa", "/business", "/review", "/stakeholder",
    "/machines", "/users", "/knowledge", "/backup", "/integrations", "/help", "/licensing", "/settings",
    "/notifications", "/lead", "/projects", "/system-check", "/support", "/cc", "/security",
)


def mount_ui(app: FastAPI, ctx) -> None:
    mount_feature_assets(app)
    mount_panel_assets(app)
    mount_console_pages(app)
    mount_logs_page(app, ctx)
    mount_help(app)
    mount_notifications_page(app, ctx)
    mount_settings_page(app, ctx)
    mount_ticket_route(app, ctx)
    mount_service_status(app)


def javascript_asset(path: Path) -> FileResponse:
    """One console script, served without caching so a release is visible on refresh."""
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


def console_feature_script() -> str:
    """Licensing notice followed by provider routing, which is what the console page loads as one file."""
    licensing = (HERE / "licensing_notice.js").read_text(encoding="utf-8")
    routing = (HERE / "provider-routing.js").read_text(encoding="utf-8")
    return licensing + "\n" + routing


def mount_feature_assets(app: FastAPI) -> None:
    @app.get("/console-assets/provider-routing.js", include_in_schema=False)
    def provider_routing_asset() -> FileResponse:
        return javascript_asset(HERE / "provider-routing.js")

    @app.get("/console-assets/console-features.js", include_in_schema=False)
    def console_features_asset() -> Response:
        return Response(console_feature_script(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/console-assets/licensing-notice.js", include_in_schema=False)
    def licensing_notice() -> FileResponse:
        return javascript_asset(HERE / "licensing_notice.js")


def mount_panel_assets(app: FastAPI) -> None:
    @app.get("/console-assets/owner-escalation.js", include_in_schema=False)
    def owner_escalation_asset() -> FileResponse:
        return javascript_asset(HERE / "owner_escalation.js")

    @app.get("/console-assets/release-readiness.js", include_in_schema=False)
    def release_readiness_asset() -> FileResponse:
        return javascript_asset(HERE / "release-readiness.js")

    @app.get("/console-assets/scale-proof.js", include_in_schema=False)
    def scale_proof_asset() -> FileResponse:
        return javascript_asset(HERE / "scale-proof.js")

    @app.get("/console-assets/flow-doctor.js", include_in_schema=False)
    def flow_doctor_asset() -> FileResponse:
        return javascript_asset(HERE / "flow-doctor.js")

    @app.get("/console-assets/run-inbox.js", include_in_schema=False)
    def run_inbox_asset() -> FileResponse:
        return javascript_asset(HERE / "run-inbox.js")

    @app.get("/console-assets/user-form-state.js", include_in_schema=False)
    def user_form_state_asset() -> FileResponse:
        return javascript_asset(HERE / "user-form-state.js")

    @app.get("/console-assets/quota-refresh-settings.js", include_in_schema=False)
    def quota_refresh_settings_asset() -> FileResponse:
        return javascript_asset(HERE / "quota_refresh_settings.js")

    @app.get("/console-assets/canary_settings.js", include_in_schema=False)
    def canary_settings_asset() -> FileResponse:
        return javascript_asset(HERE / "canary_settings.js")


def console() -> HTMLResponse:
    """The console page, with the first licensing-notice script tag pointed at the combined feature script."""
    return HTMLResponse(build_served_console_page(), headers={"Cache-Control": "no-store"})

def mount_console_pages(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    for path in CONSOLE_PAGE_PATHS:
        if path not in {"/", "/help", "/notifications", "/settings", "/logs"}:
            app.add_api_route(path, console, methods=["GET"], include_in_schema=False)


def require_console_user(request: Request, ctx, permission: str):
    """The signed-in console user, or the same refusal the guarded pages have always returned."""
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
    user = resolve_session_user(ctx.db, claims) if claims else None
    if not user:
        raise HTTPException(401, "authenticated console session required")
    if not role_satisfies(user.effective_roles, {permission}):
        raise HTTPException(403, "not available for your role")
    return user


def mount_logs_page(app: FastAPI, ctx) -> None:
    @app.get("/logs", include_in_schema=False)
    def logs(request: Request) -> FileResponse:
        require_console_user(request, ctx, "view.logs")
        return FileResponse(HERE / "logs.html", media_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})


def help_document(path: str, root: Path | None) -> FileResponse:
    """One page of the built documentation site, or the same 404 the help route has always used."""
    if root is None:
        raise HTTPException(404, "the documentation site is not installed")
    root = root.resolve()
    target = (root / (path or "index.html")).resolve()
    if target.is_dir():
        target = target / "index.html"
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "no such documentation page")
    return FileResponse(target, headers={"Cache-Control": "no-cache"})


def mount_help(app: FastAPI) -> None:
    @app.get("/help", include_in_schema=False)
    def help_root() -> RedirectResponse:
        return RedirectResponse("/help/")

    @app.get("/help/{path:path}", include_in_schema=False)
    def help_site(path: str = "") -> FileResponse:
        """The documentation site that ships in the release (docs/v4/documentation-site.md); the console Help
        links open /help/screens/<view>.html."""
        from ...core.docsite import site_dir
        return help_document(path, site_dir())


def mount_notifications_page(app: FastAPI, ctx) -> None:
    @app.get("/notifications", include_in_schema=False)
    def notifications(request: Request) -> HTMLResponse:
        require_console_user(request, ctx, "notifications.manage")
        return console()


def mount_settings_page(app: FastAPI, ctx) -> None:
    @app.get("/settings", include_in_schema=False)
    def settings_page(request: Request) -> HTMLResponse:
        require_console_user(request, ctx, "console.manage")
        return console()


def ticket_ancestors(db, ticket: dict, ticket_repository=None) -> list:
    """The ticket plus at most seven parents. The walk stops when a parent row is missing."""
    ticket_repository = ticket_repository or TicketRepository(db)
    ancestors = [ticket]
    parent = str(ticket.get("parent_key") or "")
    while parent and len(ancestors) < 8:
        ancestor = ticket_repository.ticket_ancestor(parent)
        if not ancestor:
            break
        ancestors.append(ancestor)
        parent = str(ancestor.get("parent_key") or "")
    return ancestors


def present_runs(runs: list) -> list:
    """Run ids as text and costs as floats, so the ticket payload can be encoded as JSON."""
    for row in runs:
        row["id"] = str(row["id"])
        row["cost_usd"] = float(row["cost_usd"])
    return runs


def capacity_view(decision: dict | None) -> dict | None:
    """The latest dev capacity row, or None when that run did not store an estimate."""
    if not decision:
        return None
    return {
        "account": decision["account_id"], "provider": decision["provider"],
        "assigned_at": decision["started_at"].isoformat(), "estimate": decision.get("estimate") or {},
        "reason": decision.get("reason") or "", "account_fit": decision.get("account_fit") or {}}


def ticket_detail(ctx, key: str) -> dict:
    """Ticket drawer payload: the row, its epic, runs, events, writes, timeline and merge facts."""
    t = ticket_repository_of(ctx).ticket_detail(key)
    if not t:
        raise HTTPException(404, "unknown ticket")
    from ...core.controlrepo import epic_for_ticket
    project = ctx.repo().projects.get(t["project"])
    ancestors = ticket_ancestors(ctx.db, t, ticket_repository_of(ctx))
    t["epic"] = epic_for_ticket(t, {item["key"]: item for item in ancestors})
    t["repository"] = project.repo_for(t) if project else ""
    runs = ctx.db.all("SELECT id, step, account_id, provider, model, node, fence, status, outcome, error_kind,"
                      " started_at, ended_at, tokens_in, tokens_out, cost_usd, branch, pr_url, head_sha, summary"
                      " FROM runs WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 20", (key,))
    decision = ctx.db.one(
        "SELECT account_id,provider,started_at,details->'capacity'->'estimate' AS estimate,"
        "details->'capacity'->'assignment_reason' AS reason,details->'capacity'->'account_fit' AS account_fit "
        "FROM runs WHERE ticket_key=%s AND step='dev' AND details->'capacity' ? 'estimate' "
        "ORDER BY started_at DESC LIMIT 1", (key,))
    events = ctx.db.all("SELECT ts, type, account, node, outcome, reason, data FROM events WHERE ticket=%s"
                        " ORDER BY id DESC LIMIT 80", (key,))
    writes = ctx.db.all("SELECT ts, target, action, actor, fence, accepted, result FROM write_audit WHERE ticket=%s"
                        " ORDER BY id DESC LIMIT 40", (key,))
    runs = present_runs(runs)
    from ..timeline import timeline
    from .. import merge_flow
    decided = capacity_view(decision)
    return {"ticket": t, "repository": t["repository"], "runs": runs, "events": events,
            "writes": writes, "timeline": timeline(ctx.db, key, ticket_repository=ticket_repository_of(ctx)),
            "capacity_decision": decided,
            "merge": merge_flow.facts(t), "moves": merge_flow.moves(ctx.db, key),
            "jira_url": f"{ctx.settings.jira_site}/browse/{key}"}


def mount_ticket_route(app: FastAPI, ctx) -> None:
    @app.get("/v1/tickets/{key}")
    def ticket(key: str) -> dict:
        return ticket_detail(ctx, key)


def service_rows(data) -> list:
    """Gatus endpoint rows: the latest result, duration in milliseconds, missing errors as an empty list."""
    out = []
    for endpoint in data:
        last = (endpoint.get("results") or [{}])[-1]
        out.append({"name": endpoint.get("name"), "group": endpoint.get("group"),
                    "up": bool(last.get("success")),
                    "ms": int((last.get("duration") or 0) / 1_000_000),
                    "errors": last.get("errors") or []})
    return out


def gatus_services() -> dict:
    """Active checks from Gatus, or an empty list when the monitor cannot be reached."""
    try:
        data = httpx.get("http://aiflow4-gatus:8080/api/v1/endpoints/statuses", timeout=5).json()
    except Exception:  # noqa: BLE001
        return {"services": [], "error": "gatus unreachable"}
    return {"services": service_rows(data)}


def mount_service_status(app: FastAPI) -> None:
    @app.get("/v1/services")
    def services() -> dict:
        """Active checks from Gatus (name, group, up, response time)."""
        return gatus_services()
