"""Owner decision register: add, answer and retire a decision (moved out of api.py, JAIF-398)."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import escalations, route_deps

router = APIRouter()


class OwnerDecisionBody(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    decision: str = Field(min_length=3, max_length=3000)
    keywords: list[str] = Field(default_factory=list, max_length=8)
    patterns: list[str] = Field(default_factory=list, max_length=8)
    project: str = Field(default="", max_length=40)


class OwnerDecisionAnswerBody(BaseModel):
    choice: str = Field(default="recommended", max_length=1000)
    note: str = Field(default="", max_length=1000)


@router.post("/v1/decisions")
def decision_add(body: OwnerDecisionBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, "console.write", target="escalations", action="decision-add")
    try:
        row = escalations.add_decision(ctx.db, body.title, body.decision, body.keywords, body.patterns,
                                       body.project, user.subject)
    except re.error as exc:
        raise HTTPException(400, f"a pattern is not a valid regular expression: {exc}") from None
    route_deps.audit(ctx, "escalation", "decision-add", "", "", None, user.subject, True, f"D{row['id']} {row['title']}",
                     {"decision_id": row["id"]})
    return {"id": row["id"], "register": escalations.register(ctx.db)}


@router.post("/v1/decisions/{decision_id}/answer")
def decision_answer(decision_id: int, body: OwnerDecisionAnswerBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, "console.write", target="escalations", action="decision-answer")
    try:
        row = escalations.answer_decision(ctx, decision_id, body.choice, body.note, user.subject)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    route_deps.audit(ctx, "escalation", "decision-answer", row["ticket_key"], "", None, user.subject, True,
                     f"D{row['id']} {row['title']}", {"decision_id": row["id"]})
    return {"id": row["id"], "register": escalations.register(ctx.db)}


@router.post("/v1/decisions/{decision_id}/retire")
def decision_retire(decision_id: int, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, "console.write", target="escalations", action="decision-retire")
    row = escalations.retire_decision(ctx.db, decision_id)
    if not row:
        raise HTTPException(404, "unknown decision")
    route_deps.audit(ctx, "escalation", "decision-retire", "", "", None, user.subject, True, f"D{row['id']} {row['title']}",
                     {"decision_id": row["id"]})
    return {"id": row["id"], "register": escalations.register(ctx.db)}
