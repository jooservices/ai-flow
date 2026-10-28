"""HTTP contract for people to guide a live run and workers to receive that guidance."""
from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import licensing
from ..repositories import run_inbox

router = APIRouter()
GUIDED_STEPS = frozenset({"dev", "verify", "triage", "intake", "impact", "master"})


class InboxMessageBody(BaseModel):
    client_id: UUID = Field(default_factory=uuid4)
    kind: Literal["context", "request", "idea"] = "context"
    body: str = Field(min_length=1, max_length=8000)


def _ctx(request: Request):
    return request.app.state.ctx


def _run(ctx, run_id: UUID) -> dict:
    row = run_inbox.get_run(ctx.db, str(run_id))
    if not row:
        raise HTTPException(404, "unknown run")
    return row


def _scope(request: Request, run: dict) -> None:
    user = getattr(request.state, "console_user", None)
    if user and user.project_keys is not None and run["project"] not in user.project_keys:
        raise HTTPException(403, "run is outside your project scope")


def _actor(request: Request) -> str:
    user = getattr(request.state, "console_user", None)
    if user:
        return str(user.subject)[:200]
    if _ctx(request).settings.console_login:
        raise HTTPException(401, "authenticated console session required")
    return "Console"


def _require_writable(ctx) -> None:
    try:
        licensing.require_writable(ctx.db, ctx.settings.license_public_key)
    except licensing.LicenseError as exc:
        raise HTTPException(423, str(exc)) from None


def _message(row: dict) -> dict:
    return {**row, "id": int(row["id"]), "run_id": str(row["run_id"]), "client_id": str(row["client_id"]),
            "created_at": row["created_at"].isoformat(),
            "received_at": row["received_at"].isoformat() if row["received_at"] else None,
            "included_at": row["included_at"].isoformat() if row["included_at"] else None}


@router.get("/v1/runs/{run_id}/inbox")
def get_run_inbox(run_id: UUID, request: Request) -> dict:
    ctx = _ctx(request)
    run = _run(ctx, run_id)
    _scope(request, run)
    if run.get("step") not in GUIDED_STEPS:
        raise HTTPException(409, "this run has no AI turn checkpoint")
    rows = run_inbox.latest(ctx.db, str(run_id))
    return {"run_id": str(run_id), "status": run["status"], "messages": [_message(row) for row in rows]}


@router.post("/v1/runs/{run_id}/inbox", status_code=201)
def send_run_guidance(run_id: UUID, body: InboxMessageBody, request: Request) -> dict:
    ctx = _ctx(request)
    actor = _actor(request)
    _require_writable(ctx)
    normalized = body.body.strip()
    if not normalized:
        raise HTTPException(422, "message cannot be blank")
    with ctx.db.pool.connection() as connection:
        run = run_inbox.get_run_locked(connection, str(run_id))
        if not run:
            raise HTTPException(404, "unknown run")
        _scope(request, run)
        if run.get("step") not in GUIDED_STEPS:
            raise HTTPException(409, "this run has no AI turn checkpoint")
        if run["status"] != "running" or not ctx.leases.valid(run["ticket_key"], str(run["id"]), int(run["fence"])):
            raise HTTPException(409, "run is no longer active")
        row, created = run_inbox.create(connection, run, body.client_id, actor, body.kind, normalized)
    if created:
        ctx.events.emit("run.inbox.queued", ticket=run["ticket_key"], project=run["project"],
                        run_id=str(run_id), fence=int(run["fence"]), node=run["node"],
                        reason=f"{body.kind} from {actor}", data={"message_id": row["id"]})
    return {"message": _message(row)}
