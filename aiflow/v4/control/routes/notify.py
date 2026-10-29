"""Notification triage (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import notify, route_deps

router = APIRouter()


class TriageAlertBody(BaseModel):
    run_id: str
    fence: int
    detail: str


@router.post("/v1/notify/triage")
def triage_alert(request: Request, body: TriageAlertBody, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "notify", "triage-alert", {"chars": len(body.detail)})
    if run["step"] != "triage":
        raise HTTPException(403, "only a triage run can alert the owner")
    blocks = ctx.db.one("SELECT 1 FROM tickets WHERE status <> 'Done' AND %s::text = ANY(blocked_by) LIMIT 1",
                        (run["ticket_key"],))
    severity = "critical" if blocks else "warning"
    sent = notify.send(severity, f"{run['ticket_key']} needs owner decision", body.detail[:3000],
                       ctx.settings.jira_site.rstrip("/") + f"/browse/{run['ticket_key']}"
                       if ctx.settings.jira_site else "", ctx=ctx, alert_kind="ticket_needs_person",
                       ticket=run["ticket_key"], needed="Review the question and provide an owner decision.")
    route_deps.audit(ctx, "notify", "triage-alert", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
          ",".join(sent) or "no channel configured", {"severity": severity})
    return {"ok": True, "severity": severity, "sent": sent}


