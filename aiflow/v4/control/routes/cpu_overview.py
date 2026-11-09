"""CPU use of each machine for the Dashboard; open to everyone, like the health probe."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import cpu_overview, route_deps
from ..repositories import nodes as node_repository

router = APIRouter()


@router.get("/v1/cpu-overview")
def cpu_overview_get(request: Request) -> dict:
    """Return each registered machine's total CPU and the AI/tests/browsers/databases split."""
    ctx = route_deps.context(request)
    return cpu_overview.overview(node_repository.cpu_rows(ctx.db))
