"""Event intake (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Any

from .. import route_deps

router = APIRouter()


class EventsBody(BaseModel):
    events: list[dict[str, Any]]


@router.post("/v1/events")
def events_in(request: Request, body: EventsBody, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    n = 0
    for e in body.events[:500]:
        e = dict(e)
        t = str(e.pop("type", "node.event"))
        e["node"] = node
        ctx.events.emit(t, **e)
        n += 1
    return {"accepted": n}


