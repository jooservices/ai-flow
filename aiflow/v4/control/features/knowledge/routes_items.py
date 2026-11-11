"""Discovered routes for learned-knowledge items."""
from fastapi import APIRouter, HTTPException, Request

from ... import knowledge, route_deps
from ...platform.events.wiring import publish_for
from .bodies import KnowledgeItemActionBody, KnowledgeItemBody
from .events import KnowledgeItemModerated, KnowledgeItemSaved


router = APIRouter()


@router.get("/v1/knowledge/items")
def knowledge_items(request: Request, project: str = "", q: str = "", include_retired: bool = True) -> dict:
    ctx = route_deps.context(request)
    route_deps.console_user_for(request, "knowledge.read")
    return knowledge.dashboard(ctx.db, project, q, include_retired)


@router.post("/v1/knowledge/items")
def knowledge_item_save(body: KnowledgeItemBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, "knowledge.manage", target="knowledge", action="item_save")
    try:
        item = knowledge.save_item(ctx.db, project=body.project, scope=body.scope, epic=body.epic, kind=body.kind,
                                   title=body.title, content=body.content, actor=user.subject)
    except knowledge.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    publish_for(ctx, KnowledgeItemSaved(resource=str(item["id"]), actor=user.subject, item_id=str(item["id"]),
                                        kind=body.kind, project=body.project, scope=body.scope))
    return knowledge._pack_item(item)


@router.post("/v1/knowledge/items/{item_id}/{action}")
def knowledge_item_action(item_id: int, action: str, body: KnowledgeItemActionBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, "knowledge.manage", target="knowledge", action="item_" + action,
                                       node=str(item_id))
    try:
        item = knowledge.moderate_item(ctx.db, item_id, action=action, actor=user.subject,
                                       content=body.content, title=body.title, duplicate_id=body.duplicate_id,
                                       project=body.project, scope=body.scope or None, epic=body.epic)
    except knowledge.Invalid as exc:
        raise HTTPException(422 if "not found" not in str(exc) else 404, str(exc)) from exc
    publish_for(ctx, KnowledgeItemModerated(resource=str(item_id), actor=user.subject, item_id=item_id,
                                            action=action))
    return knowledge._pack_item(item)
