"""Node-reported Flow Doctor intervention evidence route."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import flow_doctor, flow_doctor_api, route_deps

router = APIRouter()


@router.post("/v1/nodes/{name}/operator-interventions", status_code=202)
def report_operator_intervention(name: str, body: flow_doctor_api.OperatorInterventionBody, request: Request,
                                 node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    """Accept host-audit records from the node agent; the bearer is scoped to that node."""
    ctx = route_deps.context(request)
    if name != node:
        raise HTTPException(403, "token belongs to another node")
    intervention_id = flow_doctor.record_operator_intervention(
        ctx, source_key=f"host:{node}:{body.source_key}", source=body.source, actor=body.actor,
        action=body.action, target=body.target, reason=body.reason,
        happened_at=body.happened_at, details={"node": node, **body.details})
    return {"recorded": bool(intervention_id), "id": intervention_id}
