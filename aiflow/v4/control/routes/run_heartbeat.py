"""Worker run heartbeat route, discovered by the control route registry."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import route_deps, run_inbox
from ..features.runs.bodies import FenceBody

router = APIRouter()


@router.post("/v1/runs/{run_id}/heartbeat")
def run_heartbeat(run_id: str, body: FenceBody, request: Request,
                   node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    """Renew a worker run lease, save progress, and deliver pending inbox messages."""
    ctx = route_deps.context(request)
    return run_inbox.heartbeat(ctx, route_deps.run_row(ctx, run_id), run_id, body, node)
