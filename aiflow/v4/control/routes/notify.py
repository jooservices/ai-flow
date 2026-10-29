"""Notification triage (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import owner_notifications, route_deps

router = APIRouter()


class TriageAlertBody(BaseModel):
    run_id: str
    fence: int
    decision_id: int


@router.post("/v1/notify/triage")
def triage_alert(request: Request, body: TriageAlertBody, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    """Queue the owner alert (question and link only) for the decision a triage run raised."""
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "notify", "triage-alert", {"decision_id": body.decision_id})
    if run["step"] != "triage":
        raise HTTPException(403, "only a triage run can alert the owner")
    delivery = owner_notifications.enqueue_for_run(ctx, body.decision_id, run)
    if delivery is None:
        raise HTTPException(409, "the ticket is not waiting for this owner decision")
    route_deps.audit(ctx, "notify", "triage-alert", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
                     delivery["status"], {"decision_id": body.decision_id, "sent": delivery["sent"]})
    return {"ok": True, **delivery}
