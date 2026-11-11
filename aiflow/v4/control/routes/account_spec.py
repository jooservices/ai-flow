"""The node-facing account specification route, mounted by route discovery."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import route_deps
from ..features.fleet import service

router = APIRouter()


@router.get("/v1/accounts/{account_id}/spec")
def account_spec(account_id: str, request: Request,
                 node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = request.app.state.ctx
    try:
        return service.account_specification(ctx.repo(), account_id, node)
    except service.AccountSpecRefused as refused:
        raise HTTPException(403, str(refused)) from refused
