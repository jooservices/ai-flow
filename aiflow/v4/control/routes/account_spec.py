"""The node-facing account specification route, mounted by route discovery."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import route_deps

router = APIRouter()


@router.get("/v1/accounts/{account_id}/spec")
def account_spec(account_id: str, request: Request,
                 node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = request.app.state.ctx
    repo = ctx.repo()
    acc = repo.account(account_id)
    if not acc or not acc.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    model, effort = repo.model_for(acc.provider, acc.tier)
    return {"account": acc.id, "provider": acc.provider, "tier": acc.tier, "model": model, "effort": effort}
