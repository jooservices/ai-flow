"""Ticket timeline (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import route_deps

router = APIRouter()


@router.get("/v1/tickets/{key}/timeline")
def ticket_timeline(request: Request, key: str) -> dict:
    ctx = route_deps.context(request)
    from ..timeline import timeline
    tl = timeline(ctx.db, key)
    if tl is None:
        raise HTTPException(404, "unknown ticket")
    return tl


