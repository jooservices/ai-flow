"""Canary threshold settings and deploy-lag status for the settings screen and Dashboard."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from pathlib import Path

from .. import canary, route_deps

router = APIRouter()


class CanarySettingsBody(BaseModel):
    minimum_age_s: int = Field(ge=60, le=24 * 60 * 60)
    minimum_runs: int = Field(ge=0, le=1000)
    maximum_age_s: int = Field(ge=60, le=24 * 60 * 60)


@router.get("/v1/canary/settings")
def canary_settings_get(request: Request) -> dict:
    ctx = route_deps.context(request)
    return canary.get_settings(ctx.db)


@router.put("/v1/canary/settings")
def canary_settings_put(body: CanarySettingsBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, {"console.manage"}, target="aiflow-control",
                                      action="canary_settings")
    try:
        settings = canary.set_settings(ctx.db, body.minimum_age_s, body.minimum_runs,
                                       body.maximum_age_s, user.subject)
    except ValueError as exc:
        route_deps.audit(ctx, "aiflow-control", "canary_settings", "", "", None, user.subject,
                         False, str(exc), body.model_dump())
        raise HTTPException(422, str(exc)) from exc
    route_deps.audit(ctx, "aiflow-control", "canary_settings", "", "", None, user.subject, True,
                     "saved", body.model_dump())
    ctx.events.emit("canary.settings", account=user.subject, outcome="saved",
                    reason=(f"min {body.minimum_age_s}s / {body.minimum_runs} runs / "
                            f"max {body.maximum_age_s}s"))
    return settings


@router.get("/v1/canary/status")
def canary_status_get(request: Request) -> dict:
    from .. import api as control_api
    ctx = route_deps.context(request)
    releases = Path(getattr(control_api, "RELEASES", "/srv/aiflow/control/releases"))
    return canary.control_canary_status(ctx.db, releases)
