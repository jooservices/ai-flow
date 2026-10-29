"""Fenced Jira owner-question hand-off routes and optional notification delivery."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import escalations, route_deps
from ...core.owner_escalation import MAX_OWNER_QUESTION_COMMENT_CHARS

log = logging.getLogger("aiflow4.owner_escalation_routes")
router = APIRouter()


class OwnerQuestionCommentBody(BaseModel):
    run_id: str
    fence: int
    decision_id: int
    body: str = Field(min_length=1, max_length=MAX_OWNER_QUESTION_COMMENT_CHARS)


class OwnerHandoffBody(BaseModel):
    run_id: str
    fence: int
    decision_id: int


class OwnerDecisionAnswerBody(BaseModel):
    choice: str = Field(default="recommended", max_length=1000)
    note: str = Field(default="", max_length=1000)


def _fenced(request: Request, run_id: str, fence: int, target: str, action: str, payload: dict) -> dict:
    return route_deps.fenced(route_deps.context(request), run_id, fence, target, action, payload)


def _audit(request: Request, target: str, action: str, ticket: str, body: OwnerHandoffBody, run: dict,
           result: str, payload: dict) -> None:
    route_deps.audit(route_deps.context(request), target, action, ticket, body.run_id, body.fence,
                     run["account_id"], True, result, payload)


def _triage_run(request: Request, key: str, body: OwnerHandoffBody, target: str, action: str) -> dict:
    run = _fenced(request, body.run_id, body.fence, target, action, {"decision_id": body.decision_id})
    if run["ticket_key"] != key:
        raise HTTPException(403, "run is for another ticket")
    if run["step"] != "triage":
        raise HTTPException(403, "only a triage run can complete the owner question hand-off")
    return run


@router.post("/v1/escalations/{key}/question-comment")
def owner_question_comment(key: str, body: OwnerQuestionCommentBody, request: Request,
                           _node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    run = _triage_run(request, key, body, "jira", "owner-question-comment")
    try:
        posted = escalations.post_owner_question_comment(route_deps.context(request), key, body.decision_id,
                                                        body.run_id, body.body)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - Jira must return a timestamp to authorize later answers
        log.warning("owner question comment for %s failed: %s", key, type(exc).__name__)
        raise HTTPException(502, "could not record the Jira owner question comment") from exc
    _audit(request, "jira", "owner-question-comment", key, body, run,
           posted["comment_id"] or "already recorded", {"decision_id": body.decision_id})
    return {"comment_id": posted["comment_id"], "created": posted["created"].isoformat(),
            "already_posted": posted["already_posted"]}


@router.post("/v1/escalations/{key}/complete-handoff")
def complete_owner_handoff(key: str, body: OwnerHandoffBody, request: Request,
                           _node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    run = _triage_run(request, key, body, "jira", "owner-handoff-complete")
    try:
        result = escalations.complete_owner_handoff(route_deps.context(request), key, body.decision_id, body.run_id)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from None
    _audit(request, "jira", "owner-handoff-complete", key, body, run, result["resolution"],
           {"decision_id": body.decision_id})
    return {"ok": True, **result}
