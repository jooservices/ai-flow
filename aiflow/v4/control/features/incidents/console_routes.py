"""Console routes for retained run logs and explicit coordinator handoff resolution."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ... import route_deps
from . import service

router = APIRouter()


class HandoffResolutionBody(BaseModel):
    resolution: str = Field(min_length=10, max_length=1000)


@router.post("/v1/incidents/{incident_id}/resolve-and-requeue")
def resolve_ticket_handoff(incident_id: int, body: HandoffResolutionBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user(ctx, request, "console.write", target="ticket_stuck",
                                   action="resolve_handoff", node=str(incident_id))
    return service.resolve_and_requeue(ctx, incident_id, user.subject, body.resolution)


@router.get("/v1/runs/{run_id}/diagnostic-log")
def diagnostic_log(run_id: str, request: Request) -> PlainTextResponse:
    ctx = route_deps.context(request)
    route_deps.console_user(ctx, request, "console.read")
    text = service.run_log_text(ctx, run_id)
    if text is None:
        return PlainTextResponse("No retained run log is available for this run.", status_code=404)
    return PlainTextResponse(text)
