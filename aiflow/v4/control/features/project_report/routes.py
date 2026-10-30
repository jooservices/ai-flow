"""Routes of the project_report feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from types import SimpleNamespace

from .bodies import CloseProjectBody
from fastapi import HTTPException, Query
from fastapi.responses import (HTMLResponse, PlainTextResponse)
from ... import projects as project_lifecycle
from ...report import ReportNotFound, build_report, render_csv, render_html, render_markdown


def install(app, ctx, deps) -> SimpleNamespace:
    # --- state for dashboards ---------------------------------------------------------------------------------
    @app.get("/v1/projects/{key}/versions")
    def project_versions(key: str) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        versions = ctx.db.all(
            "SELECT v.id, v.name, v.released, v.release_date, count(t.key)::int AS ticket_count,"
            " count(t.key) FILTER (WHERE t.status='Done')::int AS done_count"
            " FROM versions v LEFT JOIN ticket_versions tv ON tv.version_id=v.id"
            " LEFT JOIN tickets t ON t.key=tv.ticket_key WHERE v.project=%s"
            " GROUP BY v.id, v.name, v.released, v.release_date ORDER BY v.name, v.id", (key,))
        for version in versions:
            version["open_tickets"] = ctx.db.all(
                "SELECT t.key, t.summary, t.status FROM ticket_versions tv"
                " JOIN tickets t ON t.key=tv.ticket_key"
                " WHERE tv.version_id=%s AND t.project=%s AND t.status <> 'Done' ORDER BY t.key",
                (version["id"], key))
        return {"project": key, "versions": versions}

    @app.get("/v1/projects/{key}/report")
    def project_report(key: str, epic: str | None = None, format: str = Query(default="json")):
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        if format not in ("json", "md", "csv", "html"):
            raise HTTPException(400, "format must be json, md, csv or html")
        try:
            report = build_report(ctx, key, epic)
        except ReportNotFound as e:
            raise HTTPException(404, str(e)) from e
        if format == "md":
            filename = f"{epic or key}-progress.md"
            return PlainTextResponse(render_markdown(report), media_type="text/markdown",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "csv":
            filename = f"{epic or key}-progress.csv"
            return PlainTextResponse(render_csv(report), media_type="text/csv; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "html":
            return HTMLResponse(render_html(report))
        return report

    @app.post("/v1/projects/{key}/close")
    def close_project(key: str, body: CloseProjectBody | None = None) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        result = project_lifecycle.close_project(ctx, key, body.actor if body else "owner")
        return {"project": key, "status": result["status"], "version": result["version"],
                "report": result.get("report"), "markdown": result.get("markdown"),
                "study_case_ref": result["study_case_ref"], "created": result["created"],
                "sent_channels": result["sent_channels"]}

    @app.get("/v1/projects/{key}/study-cases")
    def project_study_cases(key: str, limit: int = Query(50, ge=1, le=100)) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        cases = project_lifecycle.study_cases(ctx.db, key, limit)
        return {"project": key, "study_cases": cases}

    return SimpleNamespace(project_versions=project_versions, project_report=project_report, close_project=close_project, project_study_cases=project_study_cases)
