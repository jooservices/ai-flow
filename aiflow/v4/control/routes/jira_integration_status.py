"""Owner-protected Jira connection status read."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import jira_connect, route_deps
from ..console_auth import checks_bearer

router = APIRouter()


@router.get("/v1/integrations/jira")
@checks_bearer
def jira_status(request: Request) -> dict:
    ctx = route_deps.context(request)
    jira_connect._require_jira_owner(ctx, request)
    return jira_connect._jira_auth(ctx).status()
