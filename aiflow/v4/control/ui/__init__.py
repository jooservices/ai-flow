"""Dashboards: Command Center (/cc), Delivery view (/delivery) and Business view (/business), one page fed by
/v1/state (the Business view adds /v1/business/<project>/change-requests, see control/business.py).

The page is the console designed in the demo, rendering the live registry instead of a simulation. Extra
read-only endpoints give the drawers their detail (runs, events, audited writes and time segments of one ticket).
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
        return RedirectResponse("/cc")

    @app.get("/cc", include_in_schema=False)
    @app.get("/delivery", include_in_schema=False)
    @app.get("/business", include_in_schema=False)
    @app.get("/review", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/v1/tickets/{key}")
    def ticket(key: str) -> dict:
        t = ctx.db.one("SELECT * FROM tickets WHERE key=%s", (key,))
        if not t:
            raise HTTPException(404, "unknown ticket")
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
        return {"ticket": t, "runs": runs, "events": events, "writes": writes, "timeline": timeline(ctx.db, key),
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
