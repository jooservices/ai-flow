"""Authenticated Dashboard endpoint for developer-run timeout analytics."""
from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Query, Request

from .analytics import timeouts


def install(app: FastAPI, ctx, console_user: Callable, optional_console_user: Callable) -> None:
    @app.get("/v1/dashboard/timeouts")
    def dashboard_timeouts(request: Request, days: int = Query(30, ge=1, le=90)) -> dict:
        """Developer timeout counts are visible only for projects in the caller's Dashboard scope."""
        user = (console_user(request, "view.dashboard") if ctx.settings.console_login
                else optional_console_user(request))
        projects = user.project_keys if user and user.project_keys is not None else None
        return timeouts(ctx.db, days, projects)
