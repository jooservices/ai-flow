"""Escalation overview, raise and triage (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import escalations, route_deps

router = APIRouter()


class EscalationRaiseBody(BaseModel):
    run_id: str
    fence: int
    question: str = Field(min_length=1, max_length=6000)


class TriageDecisionBody(BaseModel):
    run_id: str
    fence: int
    decision: str
    question: str = ""
    answer: str = ""
    why: str = ""
    escalation_class: str = ""
    options: list[str] = Field(default_factory=list)
    recommended: str = ""
    topic: str = ""
    keywords: list[str] = Field(default_factory=list)
    criterion: str = ""
    narrowed: str = ""


@router.post("/v1/escalations/raise")
def escalation_raise(request: Request, body: EscalationRaiseBody, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    """A developer run stopped on a question: classify it and resolve the classes that never need a person."""
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "escalation", "raise", {"chars": len(body.question)})
    if run["step"] != "dev":
        raise HTTPException(403, "only a developer run raises an escalation here")
    result = escalations.dev_question(ctx, run, body.question)
    route_deps.audit(ctx, "escalation", "raise", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
          result["class"], {"resolved": result["resolved"], "outcome": result["outcome"]})
    ctx.events.emit("escalation.raised", ticket=run["ticket_key"], project=run["project"], run_id=body.run_id,
                    account=run["account_id"], node=node, outcome=result["class"],
                    reason=("resolved without a person: " if result["resolved"] else "goes to triage: ") + result["reason"])
    return result


@router.post("/v1/escalations/triage")
def escalation_triage(request: Request, body: TriageDecisionBody, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    """The triage AI's decision goes through the register: applied, amended, asked once, or refused."""
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "escalation", "triage", {"decision": body.decision})
    if run["step"] != "triage":
        raise HTTPException(403, "only a triage run decides an escalation")
    if body.decision not in ("ANSWER", "ESCALATE", "AMEND"):
        raise HTTPException(400, "decision must be ANSWER, ESCALATE or AMEND")
    decision = {"decision": body.decision, "answer": body.answer, "why": body.why, "class": body.escalation_class,
                "options": body.options, "recommended": body.recommended, "topic": body.topic,
                "keywords": body.keywords, "criterion": body.criterion, "narrowed": body.narrowed}
    result = escalations.triage_decision(ctx, run, decision, body.question)
    route_deps.audit(ctx, "escalation", "triage", run["ticket_key"], body.run_id, body.fence, run["account_id"],
          result["action"] != "refused", f"{result['action']}: {result.get('class', '')}",
          {"decision": body.decision, "decision_id": result.get("decision_id")})
    ctx.events.emit("escalation.triaged", ticket=run["ticket_key"], project=run["project"], run_id=body.run_id,
                    account=run["account_id"], node=node, outcome=result["action"], reason=result.get("class", ""))
    return result


@router.get("/v1/escalations")
def escalation_overview(request: Request, weeks: int = Query(8, ge=1, le=52)) -> dict:
    ctx = route_deps.context(request)
    route_deps.console_user_for(request, "console.read")
    return {"metrics": escalations.weekly_metrics(ctx.db, weeks), "register": escalations.register(ctx.db)}


