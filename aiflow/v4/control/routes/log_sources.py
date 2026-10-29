"""Log source list (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import log_viewer, route_deps

router = APIRouter()


@router.get("/v1/log-sources")
def log_sources(request: Request) -> JSONResponse:
    ctx = route_deps.context(request)
    route_deps.console_user_for(request, "view.logs")
    return JSONResponse({"sources": log_viewer.source_catalog(ctx)},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


