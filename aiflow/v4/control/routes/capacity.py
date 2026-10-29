"""Capacity planning read model (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import capacity, quota as quota_reporting, route_deps
from ..console_auth import has_permission

router = APIRouter()


@router.get("/v1/capacity")
def capacity_plan(request: Request) -> dict:
    """Next-week quota capacity, recorded assignment reasons and completed estimate errors."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.machines_ai") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    if user and not has_permission(user.effective_roles, "view.machines_ai"):
        raise HTTPException(403, "not available for your role")
    accounts = ctx.db.all("SELECT id,provider,tier,roles,quota,quota_source,quota_observed_at,quota_forecast"
                          " FROM accounts ORDER BY id")
    forecasts = {str(row["id"]): row.get("quota_forecast") or {} for row in accounts}
    # Forecasts normally come from the watchdog. On a fresh install, derive only the
    # accounts that already reported quota; accounts with no quota data cannot improve
    # this plan and should not trigger historical scans per console request.
    missing = [row for row in accounts if not forecasts[str(row["id"])] and row.get("quota")]
    forecasts.update(quota_reporting.assessments(ctx.db, missing) if missing else {})
    project_keys = user.project_keys if user and user.project_keys is not None else None
    return capacity.dashboard(ctx.db, accounts, forecasts, project_keys)


