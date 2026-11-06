"""Authenticated Dashboard endpoint for developer-run yield analytics."""
from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Query, Request

from .analytics import yield_metrics


def install(app: FastAPI, ctx, console_user: Callable, optional_console_user: Callable) -> None:
    @app.get("/v1/dashboard/yield")
    def dashboard_yield(request: Request, days: int = Query(14, ge=1, le=90)) -> dict:
        """Yield figures are visible only for projects in the caller's Dashboard scope."""
        user = (console_user(request, "view.dashboard") if ctx.settings.console_login
                else optional_console_user(request))
        projects = user.project_keys if user and user.project_keys is not None else None
        return yield_metrics(ctx.db, days, projects)
