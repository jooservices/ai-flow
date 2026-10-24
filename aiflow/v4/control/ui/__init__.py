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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..console_auth import COOKIE_NAME, resolve_session_user, role_satisfies, verify_session

HERE = Path(__file__).parent

# Keep the address inventory shared with the login gate and route tests; `/cc` and `/board` are view aliases.
CONSOLE_PAGE_PATHS = (
    "/", "/dashboard", "/work", "/board", "/delivery", "/sa", "/business", "/review", "/stakeholder",
    "/machines", "/users", "/knowledge", "/backup", "/integrations", "/help", "/licensing",
    "/notifications", "/lead", "/projects", "/system-check", "/support", "/cc",
)


def mount_ui(app: FastAPI, ctx) -> None:
    page = HERE / "console.html"

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    def console() -> FileResponse:
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    for path in CONSOLE_PAGE_PATHS:
        if path not in {"/", "/help", "/notifications"}:
            app.add_api_route(path, console, methods=["GET"], include_in_schema=False)

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

    @app.get("/notifications", include_in_schema=False)
    def notifications(request: Request) -> FileResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user:
            raise HTTPException(401, "authenticated console session required")
        if not role_satisfies(user.effective_roles, {"notifications.manage"}):
            raise HTTPException(403, "not available for your role")
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/v1/tickets/{key}")
    def ticket(key: str) -> dict:
        t = ctx.db.one("SELECT * FROM tickets WHERE key=%s", (key,))
        if not t:
            raise HTTPException(404, "unknown ticket")
        from ...core.controlrepo import epic_for_ticket
        project = ctx.repo().projects.get(t["project"])
        ancestors = [t]
        parent = str(t.get("parent_key") or "")
        while parent and len(ancestors) < 8:
            ancestor = ctx.db.one("SELECT key,type,parent_key FROM tickets WHERE key=%s", (parent,))
            if not ancestor:
                break
            ancestors.append(ancestor)
            parent = str(ancestor.get("parent_key") or "")
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
        for r in runs:
            r["id"] = str(r["id"])
            r["cost_usd"] = float(r["cost_usd"])
        from ..timeline import timeline
        from .. import merge_flow
        capacity_decision = None if not decision else {
            "account": decision["account_id"], "provider": decision["provider"],
            "assigned_at": decision["started_at"].isoformat(), "estimate": decision.get("estimate") or {},
            "reason": decision.get("reason") or "", "account_fit": decision.get("account_fit") or {}}
        return {"ticket": t, "repository": t["repository"], "runs": runs, "events": events,
                "writes": writes, "timeline": timeline(ctx.db, key), "capacity_decision": capacity_decision,
                "merge": merge_flow.facts(t), "moves": merge_flow.moves(ctx.db, key),
                "jira_url": f"{ctx.settings.jira_site}/browse/{key}"}

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
