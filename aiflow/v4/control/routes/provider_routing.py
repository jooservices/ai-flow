"""Provider routing settings and assignment explanation endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import route_deps

router = APIRouter()


class ProviderRoutingBody(BaseModel):
    order: list[str] | None = None
    ceilings: dict[str, int] | None = None


def _service(request: Request):
    return request.app.state.ctx.provider_routing


@router.get("/v1/provider-routing")
def provider_routing_state(request: Request) -> dict:
    return _service(request).view()


@router.get("/v1/provider-routing/decisions")
def provider_routing_decisions(request: Request, ticket: str = "", limit: int = 30, offset: int = 0) -> dict:
    """Return recent assignment explanations, optionally scoped to one ticket."""
    return _service(request).decisions(limit, offset, ticket)


@router.put("/v1/provider-routing/settings")
def provider_routing_settings(body: ProviderRoutingBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, {"console.manage"}, target="aiflow-control",
                                       action="provider_routing_settings")
    try:
        view = _service(request).save_settings(body.order, body.ceilings, user.subject)
    except ValueError as exc:
        route_deps.audit(ctx, "aiflow-control", "provider_routing_settings", "", "", None,
                         user.subject, False, str(exc), body.model_dump())
        raise HTTPException(422, str(exc)) from exc
    route_deps.audit(ctx, "aiflow-control", "provider_routing_settings", "", "", None,
                     user.subject, True, "saved", body.model_dump())
    ctx.events.emit("routing.settings", account=user.subject, outcome="saved",
                    reason="provider order " + ", ".join(view["order"]) + "; ceilings "
                    + ", ".join(f"{provider} {value}%" for provider, value in
                                 sorted(view["settings"]["ceilings"].items())))
    return view
