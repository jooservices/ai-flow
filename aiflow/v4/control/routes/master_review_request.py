"""Dashboard request for the next Master review."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import route_deps
from ..features.runs import master_cycles

router = APIRouter()


@router.post("/v1/master/review-requests")
def master_review_request(request: Request) -> dict:
    ctx = route_deps.context(request)
    route_deps.console_user_for(request, "console.write", target="master", action="review-request")
    master_cycles.request_review(ctx)
    return {"requested": True}
