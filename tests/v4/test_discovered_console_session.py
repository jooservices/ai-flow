"""Characterization and discovery tests for the standalone console session route."""
from concurrent.futures import ThreadPoolExecutor
from copy import copy

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control.api import create_app
from aiflow.v4.control import login
from aiflow.v4.control.console_auth import (CANONICAL_ROLES, ROLE_LABELS, ROLE_PERMISSIONS, issue_session,
                                             landing_page)
from aiflow.v4.control.route_registry import concrete_routes

SECRET = "jaif296-session-characterization-secret"
EXPIRES = 4_102_444_800


def test_open_console_session_responses_are_pinned_before_the_move(ctx):
    """Pin the current open-console response, including every session field and role option."""
    ctx.settings.console_login = False
    ctx.settings.console_session_secret = SECRET
    client = TestClient(create_app(ctx))

    unauthenticated = client.get("/v1/console/session")
    assert unauthenticated.status_code == 200
    assert unauthenticated.headers["content-type"] == "application/json"
    assert "set-cookie" not in unauthenticated.headers
    assert unauthenticated.json() == {"authenticated": False}

    token = issue_session("person@example.test", ["business", "reviewer"], EXPIRES, SECRET)
    client.cookies.set("aiflow4_session", token)
    authenticated = client.get("/v1/console/session")
    permissions = sorted(ROLE_PERMISSIONS["business"] | ROLE_PERMISSIONS["reviewer"])
    expected = {
        "authenticated": True,
        "user": "person@example.test",
        "role": "business",
        "role_label": ROLE_LABELS["business"],
        "roles": ["business", "reviewer"],
        "role_labels": [ROLE_LABELS["business"], ROLE_LABELS["reviewer"]],
        "permissions": permissions,
        "landing_page": landing_page(("business",)),
        "can_view_as": False,
        "viewing_as": None,
        "role_options": [
            {"id": role, "label": ROLE_LABELS[role], "permissions": sorted(ROLE_PERMISSIONS[role]),
             "landing_page": landing_page((role,))}
            for role in CANONICAL_ROLES
        ],
    }
    assert authenticated.status_code == 200
    assert authenticated.headers["content-type"] == "application/json"
    assert "set-cookie" not in authenticated.headers
    assert authenticated.json() == expected


def test_session_route_is_discovered_once_and_keeps_its_handler(ctx):
    app = create_app(ctx)
    matching = [route for route in concrete_routes(app.routes)
                if route.path == "/v1/console/session" and "GET" in route.methods]

    assert len(matching) == 1
    assert matching[0].name == "console_session"
    assert matching[0].endpoint.__module__ == "aiflow.v4.control.routes.console_session"
    assert matching[0].response_model is dict


def test_two_app_contexts_keep_login_settings_isolated_during_parallel_requests(ctx):
    open_ctx = copy(ctx)
    open_ctx.settings = copy(ctx.settings)
    open_ctx.settings.console_login = False
    open_ctx.settings.console_session_secret = SECRET
    gated_ctx = copy(ctx)
    gated_ctx.settings = copy(ctx.settings)
    gated_ctx.settings.console_login = True
    gated_ctx.settings.console_session_secret = SECRET
    open_client = TestClient(create_app(open_ctx))
    gated_client = TestClient(create_app(gated_ctx))

    with ThreadPoolExecutor(max_workers=2) as pool:
        open_response, gated_response = pool.map(
            lambda client: client.get("/v1/console/session", headers={
                "X-AIFLOW4-Role": "administrator", "X-AIFLOW4-User": "forged@example.test"
            }), (open_client, gated_client))

    assert (open_response.status_code, open_response.json()) == (200, {"authenticated": False})
    assert (gated_response.status_code, gated_response.json()) == (
        401, {"detail": "sign in required"})


def test_gated_session_keeps_invalid_cookie_and_disabled_user_responses(ctx):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SECRET
    client = TestClient(create_app(ctx))

    missing = client.get("/v1/console/session", headers={
        "X-AIFLOW4-Role": "administrator", "X-AIFLOW4-User": "forged@example.test"
    })
    assert (missing.status_code, missing.json()) == (401, {"detail": "sign in required"})

    invalid_tokens = (
        "forged.cookie.value",
        issue_session("person@example.test", "reviewer", 1, SECRET),
    )
    for token in invalid_tokens:
        client.cookies.set("aiflow4_session", token)
        response = client.get("/v1/console/session")
        assert (response.status_code, response.json()) == (401, {"detail": "not signed in"})
        assert "set-cookie" not in response.headers

    login.seed_user(ctx.db, "disabled@example.test", "reviewer")
    ctx.db.run("UPDATE console_users SET disabled=true WHERE email=%s", ("disabled@example.test",))
    client.cookies.set("aiflow4_session", issue_session("disabled@example.test", "reviewer", EXPIRES, SECRET))
    disabled = client.get("/v1/console/session")
    assert (disabled.status_code, disabled.json()) == (401, {"detail": "not signed in"})
    assert "set-cookie" not in disabled.headers

    ctx.settings.console_session_secret = "short"
    client.cookies.set("aiflow4_session", issue_session("person@example.test", "reviewer", EXPIRES, SECRET))
    short_key = client.get("/v1/console/session")
    assert (short_key.status_code, short_key.json()) == (401, {"detail": "not signed in"})
    assert "set-cookie" not in short_key.headers


def test_session_lookup_failure_propagates_without_cookie_or_audit_write(ctx, monkeypatch):
    ctx.settings.console_login = False
    ctx.settings.console_session_secret = SECRET
    client = TestClient(create_app(ctx))
    client.cookies.set("aiflow4_session", issue_session("person@example.test", "reviewer", EXPIRES, SECRET))
    before = ctx.db.one("SELECT count(*) AS count FROM write_audit")["count"]

    def fail_lookup(*_args, **_kwargs):
        raise RuntimeError("session lookup failed")

    monkeypatch.setattr(ctx.db, "one", fail_lookup)
    with pytest.raises(RuntimeError, match="^session lookup failed$"):
        client.get("/v1/console/session")

    assert client.cookies.get("aiflow4_session")
    monkeypatch.undo()
    after = ctx.db.one("SELECT count(*) AS count FROM write_audit")["count"]
    assert after == before
