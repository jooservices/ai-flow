"""Characterization tests for the discovered knowledge item router."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.testclient import TestClient

from aiflow.v4.control import route_registry
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.features.knowledge import routes_items
from aiflow.v4.control.routes import knowledge_items as discovered


def handler(name):
    return next(route.endpoint for route in routes_items.router.routes if route.name == name)


def request_for(ctx, method="POST", path="/v1/knowledge/items"):
    app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))
    return Request({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                    "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                    "headers": [], "client": ("test", 1), "server": ("test", 80), "app": app})


def allow_console_user(monkeypatch, calls):
    def authenticate(request, permission, **kwargs):
        calls.append((request, permission, kwargs))
        return SimpleNamespace(subject="signed@example.test")

    monkeypatch.setattr(routes_items.route_deps, "console_user_for", authenticate)


def test_item_router_is_discovered_once_with_the_three_existing_routes():
    assert discovered.router is routes_items.router
    assert sum(router is routes_items.router for _priority, _name, router in route_registry.discover_routers()) == 1
    routes = [(method, route.path) for route in routes_items.router.routes if isinstance(route, APIRoute)
              for method in route.methods]
    assert routes == [
        ("GET", "/v1/knowledge/items"),
        ("POST", "/v1/knowledge/items"),
        ("POST", "/v1/knowledge/items/{item_id}/{action}"),
    ]


def test_list_uses_each_request_context_and_keeps_default_and_explicit_filters(monkeypatch):
    auth_calls, dashboard_calls = [], []
    allow_console_user(monkeypatch, auth_calls)

    def dashboard(db, project, query, include_retired):
        dashboard_calls.append((db, project, query, include_retired))
        return {"items": []}

    monkeypatch.setattr(routes_items.knowledge, "dashboard", dashboard)
    first_db, second_db = object(), object()
    first_ctx, second_ctx = SimpleNamespace(db=first_db), SimpleNamespace(db=second_db)
    first = handler("knowledge_items")(request_for(first_ctx, "GET"))
    second = handler("knowledge_items")(request_for(second_ctx, "GET"), "ALPHA", "checkout", False)

    assert first == second == {"items": []}
    assert dashboard_calls == [(first_db, "", "", True), (second_db, "ALPHA", "checkout", False)]
    assert [call[1:] for call in auth_calls] == [
        ("knowledge.read", {}), ("knowledge.read", {}),
    ]


@pytest.mark.parametrize(("title", "content"), [
    ("", "A short guide."), ("x" * 201, "A short guide."), ("A guide", "x" * 30_001),
])
def test_save_keeps_existing_pydantic_body_limits(title, content):
    app = FastAPI()
    app.include_router(routes_items.router)
    response = TestClient(app).post("/v1/knowledge/items", json={"kind": "guide", "title": title,
                                                                  "content": content})

    assert response.status_code == 422


def test_item_routes_keep_missing_and_insufficient_session_refusals(client, ctx, db):
    ctx.settings.console_session_secret = "knowledge-item-auth-session-secret-32-bytes"
    unauthenticated = client.get("/v1/knowledge/items")
    assert unauthenticated.status_code == 401
    refused = client.post("/v1/knowledge/items", json={"kind": "guide", "title": "No session",
                                                        "content": "This must not be saved."})
    assert refused.status_code == 401
    assert db.one("SELECT accepted,result FROM write_audit WHERE target='knowledge' AND action='item_save'") == {
        "accepted": False, "result": "authenticated console session required"}

    client.cookies.set("aiflow4_session", issue_session("reader@example.test", "reviewer", 2_000_000_000,
                                                          ctx.settings.console_session_secret))
    forbidden = client.post("/v1/knowledge/items", json={"kind": "guide", "title": "No permission",
                                                          "content": "This must not be saved."})
    assert forbidden.status_code == 403 and forbidden.json()["detail"] == "not available for your role"
    refused_rows = db.all("SELECT accepted,result FROM write_audit WHERE target='knowledge' AND action='item_save'")
    assert refused_rows == [
        {"accepted": False, "result": "authenticated console session required"},
        {"accepted": False, "result": "not available for your role"},
    ]
    assert db.one("SELECT count(*) AS n FROM knowledge_items") == {"n": 0}


def test_save_uses_session_subject_and_publishes_after_persistence(monkeypatch):
    auth_calls, calls = [], []
    allow_console_user(monkeypatch, auth_calls)
    db, ctx = object(), SimpleNamespace(db=object())
    ctx.db = db
    item = {"id": 12}

    def save_item(actual_db, **values):
        calls.append(("save", actual_db, values))
        return item

    monkeypatch.setattr(routes_items.knowledge, "save_item", save_item)
    monkeypatch.setattr(routes_items, "publish_for", lambda actual_ctx, event: calls.append(("publish", actual_ctx, event)))
    monkeypatch.setattr(routes_items.knowledge, "_pack_item", lambda value: calls.append(("pack", value)) or {"id": 12})
    body = routes_items.KnowledgeItemBody(project="ALPHA", scope="project", kind="guide", title="A guide",
                                          content="A short guide.", actor="body-actor")

    result = handler("knowledge_item_save")(body, request_for(ctx))

    assert result == {"id": 12}
    assert calls[0] == ("save", db, {"project": "ALPHA", "scope": "project", "epic": "", "kind": "guide",
                                      "title": "A guide", "content": "A short guide.",
                                      "actor": "signed@example.test"})
    assert calls[1][0:2] == ("publish", ctx)
    event = calls[1][2]
    assert (event.resource, event.actor, event.item_id, event.kind, event.project, event.scope) == (
        "12", "signed@example.test", "12", "guide", "ALPHA", "project")
    assert calls[2] == ("pack", item)
    assert auth_calls[0][1:] == ("knowledge.manage", {"target": "knowledge", "action": "item_save"})


def test_save_maps_domain_invalid_without_publishing(monkeypatch):
    auth_calls, calls = [], []
    allow_console_user(monkeypatch, auth_calls)
    monkeypatch.setattr(routes_items.knowledge, "save_item",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(routes_items.knowledge.Invalid("bad item")))
    monkeypatch.setattr(routes_items, "publish_for", lambda *_args: calls.append("publish"))
    body = routes_items.KnowledgeItemBody(kind="guide", title="A guide", content="A short guide.")

    with pytest.raises(HTTPException) as error:
        handler("knowledge_item_save")(body, request_for(SimpleNamespace(db=object())))

    assert error.value.status_code == 422 and error.value.detail == "bad item"
    assert calls == []


def test_save_propagates_persistence_and_event_failures_in_order(monkeypatch):
    allow_console_user(monkeypatch, [])
    calls = []
    failure = RuntimeError("database unavailable")

    def fail_save(*_args, **_kwargs):
        calls.append("save")
        raise failure

    monkeypatch.setattr(routes_items.knowledge, "save_item", fail_save)
    monkeypatch.setattr(routes_items, "publish_for", lambda *_args: calls.append("publish"))
    body = routes_items.KnowledgeItemBody(kind="guide", title="A guide", content="A short guide.")
    ctx = SimpleNamespace(db=object())

    with pytest.raises(RuntimeError) as error:
        handler("knowledge_item_save")(body, request_for(ctx))
    assert error.value is failure and calls == ["save"]

    calls.clear()
    item = {"id": 9}
    monkeypatch.setattr(routes_items.knowledge, "save_item", lambda *_args, **_kwargs: calls.append("save") or item)

    def fail_publish(*_args):
        calls.append("publish")
        raise failure

    monkeypatch.setattr(routes_items, "publish_for", fail_publish)
    with pytest.raises(RuntimeError) as error:
        handler("knowledge_item_save")(body, request_for(ctx))
    assert error.value is failure and calls == ["save", "publish"]


def test_moderation_uses_session_actor_and_preserves_domain_event_order(monkeypatch):
    auth_calls, calls = [], []
    allow_console_user(monkeypatch, auth_calls)
    db, ctx = object(), SimpleNamespace(db=object())
    ctx.db = db
    item = {"id": 23}

    def moderate(actual_db, item_id, **values):
        calls.append(("moderate", actual_db, item_id, values))
        return item

    monkeypatch.setattr(routes_items.knowledge, "moderate_item", moderate)
    monkeypatch.setattr(routes_items, "publish_for", lambda actual_ctx, event: calls.append(("publish", actual_ctx, event)))
    monkeypatch.setattr(routes_items.knowledge, "_pack_item", lambda value: calls.append(("pack", value)) or {"id": 23})
    body = routes_items.KnowledgeItemActionBody(content="corrected", actor="body-actor")

    result = handler("knowledge_item_action")(23, "approve", body,
                                               request_for(ctx, path="/v1/knowledge/items/23/approve"))

    assert result == {"id": 23}
    assert calls[0] == ("moderate", db, 23, {"action": "approve", "actor": "signed@example.test",
                                               "content": "corrected", "title": "", "duplicate_id": None,
                                               "project": "", "scope": None, "epic": ""})
    event = calls[1][2]
    assert (event.resource, event.actor, event.item_id, event.action) == ("23", "signed@example.test", 23, "approve")
    assert calls[2] == ("pack", item)
    assert auth_calls[0][1:] == ("knowledge.manage", {"target": "knowledge", "action": "item_approve", "node": "23"})


@pytest.mark.parametrize(("message", "status"), [("item not found", 404), ("unknown action", 422)])
def test_moderation_keeps_not_found_and_invalid_action_statuses(monkeypatch, message, status):
    allow_console_user(monkeypatch, [])
    published = []

    def reject(*_args, **_kwargs):
        raise routes_items.knowledge.Invalid(message)

    monkeypatch.setattr(routes_items.knowledge, "moderate_item", reject)
    monkeypatch.setattr(routes_items, "publish_for", lambda *_args: published.append(True))
    body = routes_items.KnowledgeItemActionBody()

    with pytest.raises(HTTPException) as error:
        handler("knowledge_item_action")(7, "bad", body, request_for(SimpleNamespace(db=object())))

    assert error.value.status_code == status and error.value.detail == message
    assert not published
