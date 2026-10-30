"""Jira connection, projects and fields (moved out of api.py)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Any

from .. import merge_flow, route_deps

log = logging.getLogger("aiflow4.api")  # the logger name the handlers always used
router = APIRouter()


class JiraWrite(BaseModel):
    run_id: str
    fence: int
    to: str = ""
    body: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


@router.post("/v1/jira/{key}/transition")
def jira_transition(request: Request, key: str, body: JiraWrite, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "jira", "transition", {"to": body.to})
    if run["ticket_key"] != key:
        raise HTTPException(403, "run is for another ticket")
    if run["step"] == "impact":
        route_deps.audit(ctx, "jira", "transition", key, body.run_id, body.fence, run["account_id"], False,
              "an impact run only comments", {"to": body.to})
        raise HTTPException(403, "an impact run only comments; the owner moves the ticket")
    previous = ctx.db.one("SELECT project, type, status, labels, summary, fields FROM tickets WHERE key=%s", (key,))
    status = ctx.jira.transition_to(key, body.to)
    ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done'"
               " THEN now() ELSE done_at END, synced_at=now() WHERE key=%s", (status, status, key))
    route_deps.audit(ctx, "jira", "transition", key, body.run_id, body.fence, run["account_id"], True, status, {"to": body.to})
    ctx.events.emit("jira.transition", ticket=key, run_id=body.run_id, fence=body.fence, account=run["account_id"],
                    node=node, outcome=status)
    if previous and previous["status"] != status:
        merged_by_verify = run["step"] == "verify" and status == "Done"
        merge_flow.record_move(ctx, key, previous["status"], status,
                               merge_flow.FLOW_MERGE if merged_by_verify else merge_flow.FLOW,
                               f"{run['step']} run by {run['account_id']}")
    return {"status": status}


@router.post("/v1/jira/{key}/comment")
def jira_comment(request: Request, key: str, body: JiraWrite, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "jira", "comment", {"chars": len(body.body)})
    if run["ticket_key"] != key:
        raise HTTPException(403, "run is for another ticket")
    cid = ctx.jira.comment(key, body.body)
    route_deps.audit(ctx, "jira", "comment", key, body.run_id, body.fence, run["account_id"], True, cid, {"chars": len(body.body)})
    return {"comment_id": cid}


@router.post("/v1/jira/{key}/fields")
def jira_fields(request: Request, key: str, body: JiraWrite, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "jira", "fields", {"fields": list(body.fields), "add": body.add_labels})
    if run["ticket_key"] != key:
        raise HTTPException(403, "run is for another ticket")
    if run["step"] == "impact":
        raise HTTPException(403, "an impact run only comments")
    if body.fields:
        ctx.jira.set_fields(key, body.fields)
    for lab in body.add_labels:
        ctx.jira.add_label(key, lab)
        ctx.db.run("UPDATE tickets SET labels=CASE WHEN %s = ANY(labels) THEN labels ELSE array_append(labels,%s) END"
                   " WHERE key=%s", (lab, lab, key))
    for lab in body.remove_labels:
        ctx.jira.remove_label(key, lab)
        ctx.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (lab, key))
    route_deps.audit(ctx, "jira", "fields", key, body.run_id, body.fence, run["account_id"], True, "ok",
          {"fields": list(body.fields), "add": body.add_labels, "remove": body.remove_labels})
    return {"ok": True}


@router.post("/v1/jira/{key}/description")
def jira_description(request: Request, key: str, body: JiraWrite, node: str = Depends(route_deps.node_auth_dependency)) -> dict:
    ctx = route_deps.context(request)
    run = route_deps.fenced(ctx, body.run_id, body.fence, "jira", "description", {"chars": len(body.body)})
    if run["ticket_key"] != key:
        raise HTTPException(403, "run is for another ticket")
    if run["step"] != "intake":
        raise HTTPException(403, "only an intake run replaces the description")
    if not body.body.strip():
        raise HTTPException(400, "the new description is empty")
    ctx.jira.set_description(key, body.body)
    ctx.db.run("UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s", (body.body, key))
    route_deps.audit(ctx, "jira", "description", key, body.run_id, body.fence, run["account_id"], True, "replaced",
          {"chars": len(body.body)})
    ctx.events.emit("jira.description", ticket=key, run_id=body.run_id, fence=body.fence, account=run["account_id"],
                    node=node, outcome="replaced")
    return {"ok": True}


