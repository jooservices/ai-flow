"""Quota refresh endpoints, discovered with the other feature-owned control routers."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .. import quota_refresh

router = APIRouter()


class QuotaRefreshSettingsBody(BaseModel):
    interval_minutes: int = Field(ge=1, le=1440)


class QuotaRefreshResultBody(BaseModel):
    lease_token: uuid.UUID
    ok: bool
    observed_at: datetime | None = None
    quota: dict[str, Any] = Field(default_factory=dict)
    error: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=200)


def _node_auth(request: Request, authorization: str = Header(default="")) -> str:
    return request.app.state.control_api_auth["node_auth"](authorization)


def _console_user(request: Request, permissions: set[str] | str | None, *, action: str = "read"):
    return request.app.state.control_api_auth["console_user"](
        request, permissions, target="aiflow-control", action=action)


def _audit(request: Request, action: str, actor: str, accepted: bool, result: str, payload: dict) -> None:
    request.app.state.control_api_auth["audit"](
        "aiflow-control", action, "", "", None, actor, accepted, result, payload)


@router.post("/v1/accounts/{account_id}/quota-refresh/claim")
def account_quota_refresh_claim(account_id: str, request: Request, node: str = Depends(_node_auth)) -> dict:
    ctx = request.app.state.ctx
    acc = ctx.repo().account(account_id)
    if not acc or not acc.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    job = quota_refresh.claim(ctx.db, account_id, node)
    if not job:
        return {"job": None}
    return {"job": {"account": account_id, "provider": acc.provider,
                     "lease_token": str(job["lease_token"]), "claimed_at": job["claimed_at"]}}


@router.post("/v1/accounts/{account_id}/quota-refresh/result")
def account_quota_refresh_result(account_id: str, body: QuotaRefreshResultBody, request: Request,
                                 node: str = Depends(_node_auth)) -> dict:
    ctx = request.app.state.ctx
    acc = ctx.repo().account(account_id)
    if not acc or not acc.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    if body.ok and (not body.quota.get("windows") or body.quota.get("provider") != acc.provider):
        raise HTTPException(422, "quota refresh must contain provider windows from the matching CLI")
    recorded = quota_refresh.finish(ctx.db, account_id, body.lease_token, ok=body.ok,
                                    error=body.error, source="cli", quota=body.quota,
                                    model=body.model, observed_at=body.observed_at)
    if not recorded:
        raise HTTPException(409, "quota refresh lease expired or was already completed")
    ctx.events.emit("account.quota_refresh", account=account_id, provider=acc.provider, node=node,
                    outcome="ok" if body.ok else "failed", reason=body.error[:200])
    return {"recorded": True}


@router.get("/v1/quota-refresh/settings")
def quota_refresh_settings_get(request: Request) -> dict:
    _console_user(request, {"console.manage"})
    ctx = request.app.state.ctx
    return {"settings": quota_refresh.get_settings(ctx.db), "jobs": quota_refresh.status(ctx.db)}


@router.put("/v1/quota-refresh/settings")
def quota_refresh_settings_put(body: QuotaRefreshSettingsBody, request: Request) -> dict:
    user = _console_user(request, {"console.manage"}, action="quota_refresh_settings")
    ctx = request.app.state.ctx
    try:
        settings = quota_refresh.set_settings(ctx.db, body.interval_minutes, user.subject)
    except ValueError as exc:
        _audit(request, "quota_refresh_settings", user.subject, False, str(exc), body.model_dump())
        raise HTTPException(422, str(exc)) from exc
    _audit(request, "quota_refresh_settings", user.subject, True, "saved", body.model_dump())
    ctx.events.emit("quota_refresh.settings", account=user.subject, outcome="saved",
                    reason=f"refresh every {body.interval_minutes} minutes")
    return {"settings": settings, "jobs": quota_refresh.status(ctx.db)}
