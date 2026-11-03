"""Install the learned-knowledge item routes."""
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ... import knowledge
from ...platform.events.wiring import publish_for
from .bodies import KnowledgeItemActionBody, KnowledgeItemBody
from .events import KnowledgeItemModerated, KnowledgeItemSaved


def install_item_routes(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user

    @app.get("/v1/knowledge/items")
    def knowledge_items(request: Request, project: str = "", q: str = "", include_retired: bool = True) -> dict:
        console_user(request, "knowledge.read")
        return knowledge.dashboard(ctx.db, project, q, include_retired)

    @app.post("/v1/knowledge/items")
    def knowledge_item_save(body: KnowledgeItemBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_save")
        try:
            item = knowledge.save_item(ctx.db, project=body.project, scope=body.scope, epic=body.epic, kind=body.kind,
                                       title=body.title, content=body.content, actor=user.subject)
        except knowledge.Invalid as exc:
            raise HTTPException(422, str(exc)) from exc
        publish_for(ctx, KnowledgeItemSaved(resource=str(item["id"]), actor=user.subject, item_id=str(item["id"]),
                                            kind=body.kind, project=body.project, scope=body.scope))
        return knowledge._pack_item(item)

    @app.post("/v1/knowledge/items/{item_id}/{action}")
    def knowledge_item_action(item_id: int, action: str, body: KnowledgeItemActionBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_" + action,
                            node=str(item_id))
        try:
            item = knowledge.moderate_item(ctx.db, item_id, action=action, actor=user.subject,
                                           content=body.content, title=body.title,
                                           duplicate_id=body.duplicate_id, project=body.project,
                                           scope=body.scope or None, epic=body.epic)
        except knowledge.Invalid as exc:
            raise HTTPException(422 if "not found" not in str(exc) else 404, str(exc)) from exc
        publish_for(ctx, KnowledgeItemModerated(resource=str(item_id), actor=user.subject, item_id=item_id,
                                                action=action))
        return knowledge._pack_item(item)

    return SimpleNamespace(knowledge_items=knowledge_items, knowledge_item_save=knowledge_item_save,
                           knowledge_item_action=knowledge_item_action)
