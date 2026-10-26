"""Fenced intake route for accepting and applying a saved ticket split plan."""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import ticket_splitting
from ..core.ticket_splitting import MAX_CHILDREN


class SplitTicket(BaseModel):
    model_config = {"extra": "forbid"}
    summary: str = Field(min_length=5, max_length=240)
    description: str = Field(min_length=1, max_length=10_000)


class SplitBody(BaseModel):
    model_config = {"extra": "forbid"}
    run_id: str = Field(min_length=1, max_length=64)
    fence: int = Field(ge=1)
    tickets: list[SplitTicket] = Field(min_length=2, max_length=MAX_CHILDREN)


def install(app: FastAPI, ctx, node_auth: Callable, fenced: Callable, audit: Callable) -> None:
    @app.post("/v1/jira/{key}/split")
    def jira_split(key: str, body: SplitBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "split", {"tickets": len(body.tickets)})
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        if run["step"] != "intake":
            raise HTTPException(403, "only an intake run can split a ticket")
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        try:
            result = ticket_splitting.split_ticket(ctx, run, [item.model_dump() for item in body.tickets])
        except ticket_splitting.SplitError as exc:
            raise HTTPException(422, str(exc)) from exc
        audit("jira", "split", key, body.run_id, body.fence, run["account_id"], True,
              "split complete", {"children": result["children"]})
        ctx.events.emit("ticket.split", ticket=key, project=run["project"], run_id=body.run_id,
                        fence=body.fence, account=run["account_id"], node=node,
                        reason=";".join(result["children"]))
        return result
