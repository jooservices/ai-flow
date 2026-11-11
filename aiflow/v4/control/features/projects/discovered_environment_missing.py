"""Discovered router for missing project environment reports."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ... import environment as environment_reports
from ... import route_deps


router = APIRouter()


@router.get("/v1/environment/missing")
def environment_missing_items(request: Request) -> dict:
    """Return missing environment reports visible to the current console session."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "projects.manage") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    rows = environment_reports.reports(ctx)
    if user and user.project_keys is not None:
        rows = [row for row in rows if row["project"] in user.project_keys]
    return {"missing": rows}
