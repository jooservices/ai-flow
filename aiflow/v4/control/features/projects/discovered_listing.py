"""Managed project listing route owned by the projects feature and mounted by route discovery."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ... import route_deps
from . import service as project_service

router = APIRouter()


@router.get("/v1/projects")
def managed_projects(request: Request) -> dict:
    """List managed projects and invalid project files visible to the current console user."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user(ctx, request, "projects.manage") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    repo = ctx.repo()
    projects = project_service.managed_project_list(ctx, repo)
    if user and user.project_keys is not None:
        projects = [project for project in projects if project["key"] in user.project_keys]
    invalid_projects = [{"file": f"{name}.yaml", "reason": reason}
                        for name, reason in sorted(repo.invalid_projects.items())
                        if not user or user.project_keys is None or name in user.project_keys]
    return {"projects": projects, "invalid_projects": invalid_projects}
