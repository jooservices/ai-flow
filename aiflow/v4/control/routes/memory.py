"""Memory search and notes (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .. import memory, projects as project_lifecycle, route_deps

router = APIRouter()


@router.get("/v1/memory/search")
def memory_search(request: Request, q: str = "", project: str = "", limit: int = 10) -> dict:
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.knowledge") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    if user and user.project_keys is not None:
        if project:
            if project not in user.project_keys:
                raise HTTPException(403, "not available for your role")
            keys = [project]
        else:
            keys = list(user.project_keys)
        hits = [hit for key in keys for hit in memory.search(ctx.db, key, q, limit)]
        return {"project": project, "q": q, "hits": hits[:limit]}
    return {"project": project, "q": q, "hits": memory.search(ctx.db, project, q, limit)}


@router.get("/v1/memory/study-cases")
def memory_study_cases(request: Request, project: str = "", limit: int = Query(50, ge=1, le=100)) -> dict:
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.knowledge") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    if user and user.project_keys is not None:
        if project:
            if project not in user.project_keys:
                raise HTTPException(403, "not available for your role")
            keys = [project]
        else:
            keys = list(user.project_keys)
        cases = [case for key in keys for case in project_lifecycle.study_cases(ctx.db, key, limit)]
        return {"project": project, "study_cases": cases[:limit]}
    return {"project": project, "study_cases": project_lifecycle.study_cases(ctx.db, project, limit)}


