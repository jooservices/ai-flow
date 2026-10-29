"""HTTP routes for node-reported Flow Doctor evidence."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import flow_doctor


class OperatorInterventionBody(BaseModel):
    source_key: str = Field(min_length=1, max_length=240)
    source: Literal["shell", "configuration", "database", "jira", "system"]
    actor: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=120)
    target: str = Field(default="", max_length=300)
    reason: str = Field(default="", max_length=1000)
    happened_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def mount_node_routes(app: FastAPI, ctx, node_auth) -> None:
    @app.post("/v1/nodes/{name}/operator-interventions", status_code=202)
    def report_operator_intervention(name: str, body: OperatorInterventionBody,
                                     node: str = Depends(node_auth)) -> dict:
        """Accept host-audit records from the root-owned node agent; the bearer is scoped to that node."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        intervention_id = flow_doctor.record_operator_intervention(
            ctx, source_key=f"host:{node}:{body.source_key}", source=body.source, actor=body.actor,
            action=body.action, target=body.target, reason=body.reason,
            happened_at=body.happened_at, details={"node": node, **body.details})
        return {"recorded": bool(intervention_id), "id": intervention_id}
