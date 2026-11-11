"""Characterization tests for the discovered knowledge item router."""
from copy import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.testclient import TestClient

from aiflow.v4.control import knowledge, licensing, route_deps, route_registry
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.features.knowledge import routes_items
from aiflow.v4.control.routes import knowledge_items as discovered


SESSION_SECRET = "knowledge-items-router-test-session-secret"


def sign_in(client, ctx, *, email="items@example.test", secret=SESSION_SECRET):
    ctx.settings.console_session_secret = secret
    client.cookies.set("aiflow4_session", issue_session(email, "owner", 2_000_000_000, secret))


def disable_embedding(monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)


def save_body(title="A guide"):
    return {"project": "TESTWP", "scope": "project", "kind": "guide", "title": title,
            "content": "Keep the route contract stable."}


def event_count(db, event_type):
    return db.one("SELECT count(*) AS n FROM domain_events WHERE type=%s", (event_type,))["n"]


def write_count(db, action, *, accepted=None):
    query = "SELECT count(*) AS n FROM write_audit WHERE target='knowledge' AND action=%s"
    params = [action]
    if accepted is not None:
        query += " AND accepted=%s"
        params.append(accepted)
    return db.one(query, params)["n"]


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


def test_dashboard_api_pins_empty_default_filters_retired_values_and_stale_side_effect(client, ctx, db, monkeypatch):
    disable_embedding(monkeypatch)
    sign_in(client, ctx)
    shared = knowledge.save_item(db, scope="general", kind="guide", title="Checkout shared note",
                                 content="Shared checkout guidance.")
    alpha = knowledge.save_item(db, project="ALPHA", kind="guide", title="Checkout Alpha guide",
                                content="Alpha checkout guidance.")
    beta = knowledge.save_item(db, project="BETA", kind="guide", title="Checkout Beta guide",
                               content="Beta checkout guidance.")
    retired = knowledge.save_item(db, project="ALPHA", kind="guide", title="Checkout retired guide",
                                  content="Retired checkout guidance.", status="retired")
    stale = knowledge.save_item(db, project="ALPHA", kind="guide", title="Old Alpha guide",
                                content="This guide should be flagged stale by GET.")
    db.run("UPDATE knowledge_items SET updated_at=now()-interval '181 days' WHERE id=%s", (stale["id"],))

    default = client.get("/v1/knowledge/items")
    assert default.status_code == 200, default.text
    assert {row["id"] for row in default.json()["items"]} == {shared["id"]}
    assert db.one("SELECT status FROM knowledge_items WHERE id=%s", (stale["id"],))["status"] == "stale"

    project_default = client.get("/v1/knowledge/items", params={"project": "alpha"})
    assert project_default.status_code == 200, project_default.text
    assert {row["id"] for row in project_default.json()["items"]} == {
        shared["id"], alpha["id"], retired["id"], stale["id"],
    }
    included_retired = client.get("/v1/knowledge/items", params={
        "project": "ALPHA", "q": "checkout", "include_retired": True,
    })
    assert included_retired.status_code == 200, included_retired.text
    assert {row["id"] for row in included_retired.json()["items"]} == {
        shared["id"], alpha["id"], retired["id"],
    }
    excluded_retired = client.get("/v1/knowledge/items", params={
        "project": "ALPHA", "q": "checkout", "include_retired": False,
    })
    assert excluded_retired.status_code == 200, excluded_retired.text
    assert {row["id"] for row in excluded_retired.json()["items"]} == {shared["id"], alpha["id"]}
    empty = client.get("/v1/knowledge/items", params={"project": "ALPHA", "q": "no-such-checkout-note"})
    assert empty.status_code == 200 and empty.json()["items"] == []
    assert beta["id"] not in {row["id"] for row in project_default.json()["items"]}


def test_two_assembled_apps_authenticate_against_their_own_context(client, ctx):
    first_secret = "knowledge-items-first-app-session-secret"
    second_secret = "knowledge-items-second-app-session-secret"
    ctx.settings.console_session_secret = first_secret
    second_settings = copy(ctx.settings)
    second_settings.console_session_secret = second_secret
    second_ctx = replace(ctx, settings=second_settings, event_registry=None)
    second_app = create_app(second_ctx)
    second_client = TestClient(second_app)
    sign_in(client, ctx, email="two-apps@example.test", secret=first_secret)
    sign_in(second_client, second_ctx, email="two-apps@example.test", secret=second_secret)

    assert second_app.state.ctx is second_ctx and second_ctx is not ctx
    assert client.get("/v1/knowledge/items").status_code == 200
    assert second_client.get("/v1/knowledge/items").status_code == 200

    second_client.cookies.set("aiflow4_session", issue_session("two-apps@example.test", "owner", 2_000_000_000,
                                                                 first_secret))
    refused = second_client.get("/v1/knowledge/items")
    assert refused.status_code == 401


def test_missing_item_and_unknown_action_return_real_api_errors_without_moderation_events(client, ctx, db,
                                                                                           monkeypatch):
    disable_embedding(monkeypatch)
    sign_in(client, ctx)
    created = client.post("/v1/knowledge/items", json=save_body("Action error fixture"))
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]
    before_events = event_count(db, "knowledge.item_moderated")
    before_audits = write_count(db, "item_approve", accepted=True)

    missing = client.post("/v1/knowledge/items/999999/approve", json={})
    unknown = client.post(f"/v1/knowledge/items/{item_id}/unknown", json={})

    assert missing.status_code == 404 and missing.json()["detail"] == "knowledge item not found"
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown knowledge action"
    assert event_count(db, "knowledge.item_moderated") == before_events
    assert write_count(db, "item_approve", accepted=True) == before_audits
    assert write_count(db, "item_unknown", accepted=True) == 0
    assert db.one("SELECT approved,status FROM knowledge_items WHERE id=%s", (item_id,)) == {
        "approved": False, "status": "active"}


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
    assert unauthenticated.json()["detail"] == "authenticated console session required"
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
    forbidden_moderation = client.post("/v1/knowledge/items/1/approve", json={})
    assert forbidden_moderation.status_code == 403
    assert forbidden_moderation.json()["detail"] == "not available for your role"
    refused_rows = db.all("SELECT action,accepted,result FROM write_audit WHERE target='knowledge'"
                          " AND action IN ('item_save','item_approve') ORDER BY id")
    assert refused_rows == [
        {"action": "item_save", "accepted": False, "result": "authenticated console session required"},
        {"action": "item_save", "accepted": False, "result": "not available for your role"},
        {"action": "item_approve", "accepted": False, "result": "not available for your role"},
    ]
    assert db.one("SELECT count(*) AS n FROM knowledge_items") == {"n": 0}


@pytest.mark.parametrize("session", ["missing", "invalid", "expired", "disabled"])
@pytest.mark.parametrize("operation", ["list", "save", "moderate"])
def test_item_writes_refuse_invalid_expired_and_disabled_sessions_without_mutation(
        client, ctx, db, monkeypatch, session, operation):
    disable_embedding(monkeypatch)
    sign_in(client, ctx)
    item_id = None
    if operation == "moderate":
        created = client.post("/v1/knowledge/items", json=save_body("Existing item"))
        assert created.status_code == 200, created.text
        item_id = created.json()["id"]
    if session == "disabled":
        db.run("INSERT INTO console_users (email,role,roles,password_hash,disabled)"
               " VALUES ('items@example.test','administrator',%s,'test',true)", (["administrator"],))
        token = issue_session("items@example.test", "owner", 2_000_000_000, SESSION_SECRET)
    elif session == "expired":
        token = issue_session("items@example.test", "owner", 1, SESSION_SECRET)
    else:
        token = "invalid-session-cookie"
    if session == "missing":
        client.cookies.clear()
    else:
        client.cookies.set("aiflow4_session", token)

    before_items = db.one("SELECT count(*) AS n FROM knowledge_items")["n"]
    before_events = event_count(db, "knowledge.item_saved") + event_count(db, "knowledge.item_moderated")
    before_audits = db.all("SELECT action,accepted,result FROM write_audit WHERE target='knowledge' ORDER BY id")
    action = None
    if operation == "list":
        response = client.get("/v1/knowledge/items")
    elif operation == "save":
        response = client.post("/v1/knowledge/items", json=save_body("Refused item"))
        action = "item_save"
    else:
        response = client.post(f"/v1/knowledge/items/{item_id}/approve", json={})
        action = "item_approve"

    assert response.status_code == 401 and response.json()["detail"] == "authenticated console session required"
    assert db.one("SELECT count(*) AS n FROM knowledge_items")["n"] == before_items
    assert event_count(db, "knowledge.item_saved") + event_count(db, "knowledge.item_moderated") == before_events
    if action:
        assert db.one("SELECT accepted,result FROM write_audit WHERE target='knowledge' AND action=%s ORDER BY id DESC LIMIT 1",
                      (action,)) == {"accepted": False, "result": "authenticated console session required"}
    else:
        assert db.all("SELECT action,accepted,result FROM write_audit WHERE target='knowledge' ORDER BY id") == before_audits
    if operation == "moderate":
        assert db.one("SELECT approved,status FROM knowledge_items WHERE id=%s", (item_id,)) == {
            "approved": False, "status": "active"}


@pytest.mark.parametrize("operation", ["save", "moderate"])
def test_non_writable_license_refuses_item_save_and_moderation_without_audit_or_mutation(
        client, ctx, db, monkeypatch, operation):
    disable_embedding(monkeypatch)
    sign_in(client, ctx)
    item_id = None
    if operation == "moderate":
        created = client.post("/v1/knowledge/items", json=save_body("Existing item"))
        assert created.status_code == 200, created.text
        item_id = created.json()["id"]
    before_items = db.one("SELECT count(*) AS n FROM knowledge_items")["n"]
    before_saved_events = event_count(db, "knowledge.item_saved")
    before_moderated_events = event_count(db, "knowledge.item_moderated")
    before_saves = write_count(db, "item_save")
    before_moderations = write_count(db, "item_approve")

    def reject_write(*_args, **_kwargs):
        raise licensing.LicenseError("read-only license")

    monkeypatch.setattr(route_deps.licensing, "require_writable", reject_write)
    if operation == "save":
        response = client.post("/v1/knowledge/items", json=save_body("Read-only item"))
    else:
        response = client.post(f"/v1/knowledge/items/{item_id}/approve", json={})

    assert response.status_code == 423 and response.json()["detail"] == "read-only license"
    assert db.one("SELECT count(*) AS n FROM knowledge_items")["n"] == before_items
    assert event_count(db, "knowledge.item_saved") == before_saved_events
    assert event_count(db, "knowledge.item_moderated") == before_moderated_events
    assert write_count(db, "item_save") == before_saves
    assert write_count(db, "item_approve") == before_moderations
    if operation == "moderate":
        assert db.one("SELECT approved,status FROM knowledge_items WHERE id=%s", (item_id,)) == {
            "approved": False, "status": "active"}


def test_saved_item_audit_listener_http_failure_keeps_item_and_retry_creates_one_event_and_audit(
        client, ctx, db, monkeypatch):
    disable_embedding(monkeypatch)
    sign_in(client, ctx)
    original_audit = route_deps.audit

    def fail_audit(*_args, **_kwargs):
        raise HTTPException(500, "audit listener unavailable")

    monkeypatch.setattr(route_deps, "audit", fail_audit)
    first = client.post("/v1/knowledge/items", json=save_body("Retry save"))
    assert first.status_code == 500 and first.json()["detail"] == "audit listener unavailable"
    assert db.one("SELECT count(*) AS n FROM knowledge_items")["n"] == 1
    assert event_count(db, "knowledge.item_saved") == 0
    assert write_count(db, "item_save", accepted=True) == 0

    monkeypatch.setattr(route_deps, "audit", original_audit)
    retry = client.post("/v1/knowledge/items", json=save_body("Retry save"))
    assert retry.status_code == 200, retry.text
    assert db.one("SELECT count(*) AS n FROM knowledge_items")["n"] == 2
    assert event_count(db, "knowledge.item_saved") == 1
    assert write_count(db, "item_save", accepted=True) == 1


def test_moderation_audit_listener_http_failure_persists_change_and_retry_creates_one_event_and_audit(
        client, ctx, db, monkeypatch):
    disable_embedding(monkeypatch)
    sign_in(client, ctx)
    created = client.post("/v1/knowledge/items", json=save_body("Retry moderation"))
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]
    original_audit = route_deps.audit

    def fail_audit(*_args, **_kwargs):
        raise HTTPException(500, "audit listener unavailable")

    monkeypatch.setattr(route_deps, "audit", fail_audit)
    first = client.post(f"/v1/knowledge/items/{item_id}/approve", json={})
    assert first.status_code == 500 and first.json()["detail"] == "audit listener unavailable"
    assert db.one("SELECT approved,status FROM knowledge_items WHERE id=%s", (item_id,)) == {
        "approved": True, "status": "active"}
    assert event_count(db, "knowledge.item_moderated") == 0
    assert write_count(db, "item_approve", accepted=True) == 0

    monkeypatch.setattr(route_deps, "audit", original_audit)
    retry = client.post(f"/v1/knowledge/items/{item_id}/approve", json={})
    assert retry.status_code == 200, retry.text
    assert db.one("SELECT count(*) AS n FROM knowledge_items")["n"] == 1
    assert event_count(db, "knowledge.item_moderated") == 1
    assert write_count(db, "item_approve", accepted=True) == 1


@pytest.mark.parametrize("operation", ["save", "moderate"])
def test_unhandled_audit_listener_failure_rolls_back_item_write_and_event(client, ctx, db, monkeypatch, operation):
    disable_embedding(monkeypatch)
    sign_in(client, ctx)
    item_id = None
    if operation == "moderate":
        created = client.post("/v1/knowledge/items", json=save_body("Rollback moderation"))
        assert created.status_code == 200, created.text
        item_id = created.json()["id"]
    original_audit = route_deps.audit

    def crash_audit(*_args, **_kwargs):
        raise RuntimeError("audit listener crashed")

    monkeypatch.setattr(route_deps, "audit", crash_audit)
    with pytest.raises(RuntimeError, match="audit listener crashed"):
        if operation == "save":
            client.post("/v1/knowledge/items", json=save_body("Rolled back item"))
        else:
            client.post(f"/v1/knowledge/items/{item_id}/approve", json={})
    monkeypatch.setattr(route_deps, "audit", original_audit)

    if operation == "save":
        assert db.one("SELECT count(*) AS n FROM knowledge_items")["n"] == 0
        assert event_count(db, "knowledge.item_saved") == 0
        assert write_count(db, "item_save", accepted=True) == 0
    else:
        assert db.one("SELECT approved,status FROM knowledge_items WHERE id=%s", (item_id,)) == {
            "approved": False, "status": "active"}
        assert event_count(db, "knowledge.item_moderated") == 0
        assert write_count(db, "item_approve", accepted=True) == 0


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
