"""Log viewer reads (moved out of api.py)."""
from __future__ import annotations

import uuid
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import log_viewer, route_deps
from ...core import log_sources as log_sources_core
from ..db import J

router = APIRouter()


class LogReadBody(BaseModel):
    model_config = {"extra": "forbid"}
    source_id: str = Field(min_length=1, max_length=256)


@router.post("/v1/logs", status_code=202)
def request_log_read(body: LogReadBody, request: Request) -> JSONResponse:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, "view.logs")
    source = log_viewer.find_source(ctx, body.source_id)
    if source is None:
        raise HTTPException(404, "unknown log source")
    if not source["available"]:
        raise HTTPException(409, "log source is unavailable")
    operation_id = str(uuid.uuid4())
    payload = {"action": "read_logs", "source": source["id"], "kind": source["kind"],
               "target": source["target"], "lines": log_sources_core.MAX_LOG_LINES}
    with ctx.db.pool.connection() as connection:
        connection.execute("DELETE FROM node_operations WHERE action='read_logs'"
                           " AND status IN ('completed','failed') AND created_at < now() - interval '15 minutes'")
        connection.execute("INSERT INTO node_operations (id,node,action,actor,payload)"
                           " VALUES (%s,%s,'read_logs',%s,%s)",
                           (operation_id, source["node"], user.subject, J(payload)))
    return JSONResponse({"id": operation_id, "source": source["id"], "status": "queued"}, status_code=202,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/v1/logs/requests/{operation_id}")
def log_read_status(operation_id: str, request: Request) -> JSONResponse:
    ctx = route_deps.context(request)
    route_deps.console_user_for(request, "view.logs")
    try:
        parsed_id = str(uuid.UUID(operation_id))
    except ValueError as exc:
        raise HTTPException(404, "unknown log request") from exc
    row = ctx.db.one("SELECT id,payload,status,result FROM node_operations"
                     " WHERE id=%s AND action='read_logs'"
                     " AND created_at >= now() - interval '15 minutes'", (parsed_id,))
    if not row:
        raise HTTPException(404, "unknown log request")
    status = str(row["status"])
    result = row.get("result") or {}
    if status == "completed" and result.get("available"):
        lines = log_viewer.redact_log_lines(result.get("lines"))
        available = True
        message = "" if lines else "No recent log lines for this source."
    elif status in {"failed", "completed"}:
        lines, available, message = [], False, "This log source is unavailable right now."
    else:
        lines, available, message = [], None, "Waiting for the node agent to read this source."
    return JSONResponse({"id": str(row["id"]), "source": (row.get("payload") or {}).get("source", ""),
                         "status": status, "available": available, "lines": lines, "message": message},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


