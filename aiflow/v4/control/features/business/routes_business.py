"""The business view: change requests and release readiness (same access as the console, no node token)."""
from types import SimpleNamespace

from fastapi import HTTPException

from ... import business
from ...platform.events.wiring import publish_for
from .bodies import ChangeRequestBody
from .events import ChangeRequestFiled
from aiflow.v4.control.features.tickets import ticket_repository_of


def install_business_routes(app, ctx) -> SimpleNamespace:
    def business_project(key: str) -> str:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        return key

    @app.get("/v1/business/{key}/change-requests")
    def business_change_requests(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "jira_site": ctx.settings.jira_site,
                "epics": business.epics(ticket_repository_of(ctx), project),
                "change_requests": business.change_requests(ticket_repository_of(ctx), ctx.db, project,
                                                             ctx.settings.jira_site)}

    @app.get("/v1/business/{key}/release-readiness")
    def business_release_readiness(key: str) -> dict:
        project = business_project(key)
        return {"project": project,
                "versions": business.release_readiness(ctx.db, project, ticket_repository_of(ctx))}

    @app.get("/v1/business/{key}/release-progress")
    def business_release_progress(key: str) -> dict:
        project = business_project(key)
        return {"project": project,
                "versions": business.release_progress_of(ctx.db, project, ticket_repository_of(ctx))}

    @app.post("/v1/business/{key}/change-requests")
    def business_change_request(key: str, body: ChangeRequestBody) -> dict:
        project = business_project(key)
        try:
            title, description, epic = business.validate(ticket_repository_of(ctx), project, body.title,
                                                        body.description, body.epic)
        except business.Invalid as e:
            raise HTTPException(400, str(e)) from e
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        created = business.create(ctx, project, title, description, epic)
        publish_for(ctx, ChangeRequestFiled(project=project, actor=business.ACTOR, ticket=created["key"],
                                            status=created["status"], epic=epic, chars=len(description),
                                            labels=[business.LABEL]))
        ctx.events.emit("business.change_request", ticket=created["key"], project=project, outcome=created["status"],
                        reason=title[:200])
        return created

    return SimpleNamespace(business_project=business_project, business_change_requests=business_change_requests,
                           business_release_readiness=business_release_readiness,
                           business_release_progress=business_release_progress,
                           business_change_request=business_change_request)
