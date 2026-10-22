"""Dashboards: Dashboard (/dashboard, /cc), Work board (/work, /board), Delivery (/delivery), SA health (/sa),
Machines and AI (/machines), Users and roles (/users), Team Lead (/lead),
Business (/business), Human review (/review), Stakeholder (/stakeholder), Knowledge (/knowledge) and
Integrations (/integrations, control/jira_connect.py), fed by /v1/state and relevant view APIs.

All views are fed by the live registry; the Business view adds
/v1/business/<project>/change-requests (see control/business.py).
The console renders the live registry; extra read-only endpoints give its drawers ticket details, runs, events,
audited writes and time segments.
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

HERE = Path(__file__).parent


def mount_ui(app: FastAPI, ctx) -> None:
    page = HERE / "console.html"

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/cc", include_in_schema=False)
    @app.get("/board", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    @app.get("/work", include_in_schema=False)
    @app.get("/delivery", include_in_schema=False)
    @app.get("/machines", include_in_schema=False)
    @app.get("/users", include_in_schema=False)
    @app.get("/sa", include_in_schema=False)
    @app.get("/lead", include_in_schema=False)
    @app.get("/business", include_in_schema=False)
    @app.get("/review", include_in_schema=False)
    @app.get("/stakeholder", include_in_schema=False)
    @app.get("/knowledge", include_in_schema=False)
    @app.get("/integrations", include_in_schema=False)
    @app.get("/projects", include_in_schema=False)
    def console() -> FileResponse:
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
        events = ctx.db.all("SELECT ts, type, account, node, outcome, reason, data FROM events WHERE ticket=%s"
                            " ORDER BY id DESC LIMIT 80", (key,))
        writes = ctx.db.all("SELECT ts, target, action, actor, fence, accepted, result FROM write_audit WHERE ticket=%s"
                            " ORDER BY id DESC LIMIT 40", (key,))
        for r in runs:
            r["id"] = str(r["id"])
            r["cost_usd"] = float(r["cost_usd"])
        from ..timeline import timeline
        return {"ticket": t, "repository": t["repository"], "runs": runs, "events": events,
                "writes": writes, "timeline": timeline(ctx.db, key),
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
