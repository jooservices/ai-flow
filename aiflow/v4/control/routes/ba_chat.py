"""BA chat candidates and results (moved out of api.py)."""
from __future__ import annotations

import logging

import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Any

from .. import ba_chat, licensing, route_deps

log = logging.getLogger("aiflow4.api")  # the logger name the handlers always used
router = APIRouter()


class BaChatResultBody(BaseModel):
    account: str = ""
    claim_token: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    error_kind: str = ""


@router.get("/v1/ba-chat/candidate")
def ba_chat_candidate(request: Request, account: str, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    try:
        licensing.enforce_work_entitlement(ctx, account, node, public_key=ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        return {"job": None, "reason": str(exc)}
    try:
        return {"job": ba_chat.claim_job(ctx, account, node)}
    except Exception as exc:  # noqa: BLE001 - poll failures must not expose chat text
        log.warning("BA chat claim failed account=%s error=%s", account, type(exc).__name__)
        return {"job": None}


@router.post("/v1/ba-chat/{job_id}/result")
def ba_chat_result(request: Request, job_id: str, body: BaChatResultBody, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    try:
        parsed_id = str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(404, "chat turn was not found") from None
    if not ba_chat.complete_job(ctx, parsed_id, body.claim_token, body.account, node,
                                body.output, body.error_kind):
        raise HTTPException(409, "chat turn is no longer assigned to this worker")
    return {"accepted": True}


