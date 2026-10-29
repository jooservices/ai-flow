"""Role health read models and actions (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import role_health, route_deps

router = APIRouter()


class RoleHealthSettingsBody(BaseModel):
    window_runs: int
    failure_percent: int


@router.get("/v1/role-health")
def role_health_state(request: Request) -> dict:
    ctx = route_deps.context(request)
    return role_health.summary(ctx)


@router.put("/v1/role-health/settings")
def role_health_settings(body: RoleHealthSettingsBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, {"console.manage"}, target="aiflow-control", action="role_health_settings")
    try:
        role_health.set_settings(ctx.db, body.window_runs, body.failure_percent, user.subject)
    except ValueError as e:
        route_deps.audit(ctx, "aiflow-control", "role_health_settings", "", "", None, user.subject, False, str(e), body.model_dump())
        raise HTTPException(422, str(e)) from e
    route_deps.audit(ctx, "aiflow-control", "role_health_settings", "", "", None, user.subject, True, "saved", body.model_dump())
    ctx.events.emit("role.settings", account=user.subject, outcome="saved",
                    reason=f"window {body.window_runs} runs, failure score above {body.failure_percent}%")
    role_health.evaluate(ctx)
    return role_health.summary(ctx)


