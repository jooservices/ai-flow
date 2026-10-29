"""Health probe (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import route_deps
from ... import VERSION

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict:
    ctx = route_deps.context(request)
    ctx.db.one("SELECT 1 AS ok")
    ctx.leases.r.ping()
    return {"ok": True, "version": VERSION}


