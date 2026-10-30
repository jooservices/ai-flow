"""Analytics read models (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from typing import Literal

from .. import conflict_loss, route_deps
from ..console_auth import ConsoleUser, has_permission
from ..platform.auth import can

router = APIRouter()


@router.get("/v1/analytics/daily")
def analytics_daily(request: Request, days: int = Query(14, ge=1, le=90), project: str = "",
                    user: ConsoleUser | None = can("view.dashboard")) -> dict:
    ctx = route_deps.context(request)
    from ..analytics import daily
    if user and user.project_keys is not None:
        if project:
            if project not in user.project_keys:
                raise HTTPException(403, "not available for your role")
            projects = [project]
        else:
            projects = list(user.project_keys)
        rows = [row for key in projects for row in daily(ctx.db, days, key)]
    else:
        rows = daily(ctx.db, days, project)
    return {"days": days, "project": project, "rows": rows}


@router.get("/v1/analytics/conflicts")
def analytics_conflicts(request: Request, days: int = Query(14, ge=1, le=90),
                        bucket: Literal["day", "week"] = "day", project: str = "",
                        user: ConsoleUser | None = can("view.dashboard")) -> dict:
    """Share of verifications lost to merge conflicts, the time spent on them and the trend."""
    ctx = route_deps.context(request)
    project_keys = user.project_keys if user and user.project_keys is not None else None
    if project and project_keys is not None and project not in project_keys:
        raise HTTPException(403, "not available for your role")
    return {"project": project, **conflict_loss.summary(ctx.db, days, bucket, project_keys, project)}


@router.get("/v1/analytics/usage/daily")
def analytics_usage_daily(request: Request, days: int = Query(14, ge=1, le=90),
                          user: ConsoleUser | None = can("view.dashboard")) -> dict:
    ctx = route_deps.context(request)
    from ..analytics import daily_usage
    project_keys = user.project_keys if user and user.project_keys is not None else None
    return {"days": days, "group_by": "ai", "rows": daily_usage(ctx.db, days, project_keys)}


@router.get("/v1/analytics/usage")
def analytics_usage(request: Request, days: int = Query(7, ge=1, le=90),
                    group_by: Literal["account", "ai", "machine", "project", "ticket"] = "account",
                    project: str = "", ticket: str = "", account: str = "") -> dict:
    ctx = route_deps.context(request)
    from ..analytics import usage
    user = (route_deps.console_user_for(request, None) if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))

    def refuse() -> None:
        if user:
            route_deps.audit(ctx, "console", "access_denied", "", "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "method": request.method,
                                                   "roles": list(user.effective_roles)})
        raise HTTPException(403, "not available for your role")

    needs_machine_access = group_by in {"account", "machine"}
    if user and needs_machine_access and not has_permission(user.effective_roles, "view.machines_ai"):
        refuse()

    project_keys = None
    if user and user.project_keys is not None:
        project_keys = user.project_keys
        if project and project not in project_keys:
            refuse()
        if ticket:
            ticket_row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket,))
            projects = ({ticket_row["project"]} if ticket_row else set())
            if not projects:
                projects = {row["project"] for row in ctx.db.all(
                    "SELECT DISTINCT project FROM runs WHERE ticket_key=%s AND project<>''", (ticket,))}
            if projects and not projects.issubset(set(project_keys)):
                refuse()

    return {"days": days, "group_by": group_by, "filters": {"project": project, "ticket": ticket,
                                                                 "account": account},
            "rows": usage(ctx.db, days, group_by, project, ticket, account, project_keys=project_keys)}


