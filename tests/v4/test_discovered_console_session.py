"""Pre-move response goldens and discovery tests for the standalone console session route."""
from concurrent.futures import ThreadPoolExecutor
from copy import copy
import json

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.route_registry import concrete_routes

SECRET = "jaif296-session-characterization-secret"
OTHER_SECRET = "jaif296-distinct-context-signing-secret"
EXPIRES = 4_102_444_800

# Captured from the pre-move route. Keep these labels, permission lists, landing pages and all nine options literal:
# deriving them from console_auth would let a production change silently update the characterization expectation.
ROLE_OPTIONS = [
    {"id": "administrator", "label": "Administrator", "permissions": ["*"], "landing_page": "/cc"},
    {"id": "product_owner", "label": "Product owner",
     "permissions": ["console.read", "knowledge.read", "review.actions", "view.business", "view.dashboard",
                     "view.delivery", "view.human_review", "view.knowledge", "view.stakeholder", "view.team_lead",
                     "view.work_board"], "landing_page": "/cc"},
    {"id": "delivery", "label": "Delivery",
     "permissions": ["console.read", "review.actions", "view.dashboard", "view.delivery", "view.human_review",
                     "view.work_board"], "landing_page": "/delivery"},
    {"id": "team_lead", "label": "Team Lead",
     "permissions": ["console.read", "knowledge.read", "node.operations.read", "review.actions", "view.dashboard",
                     "view.delivery", "view.human_review", "view.knowledge", "view.team_lead", "view.work_board"],
     "landing_page": "/lead"},
    {"id": "sa", "label": "SA",
     "permissions": ["console.read", "integrations.manage", "knowledge.read", "node.operations.read", "nodes.manage",
                     "view.dashboard", "view.integrations", "view.knowledge", "view.machines_ai", "view.sa",
                     "view.work_board"],
     "landing_page": "/sa"},
    {"id": "ba", "label": "BA",
     "permissions": ["console.read", "knowledge.read", "view.business", "view.dashboard", "view.knowledge",
                     "view.work_board"], "landing_page": "/business"},
    {"id": "business", "label": "Business",
     "permissions": ["console.read", "view.business", "view.dashboard", "view.work_board"],
     "landing_page": "/business"},
    {"id": "reviewer", "label": "Reviewer",
     "permissions": ["console.read", "review.actions", "view.dashboard", "view.human_review", "view.work_board"],
     "landing_page": "/review"},
    {"id": "stakeholder", "label": "Stakeholder",
     "permissions": ["console.read", "view.dashboard", "view.stakeholder", "view.work_board"],
     "landing_page": "/stakeholder"},
]

OPEN_ANONYMOUS = {"authenticated": False}
OPEN_REVIEWER = {
    "authenticated": True, "user": "person@example.test", "role": "reviewer", "role_label": "Reviewer",
    "roles": ["reviewer"], "role_labels": ["Reviewer"],
    "permissions": ["console.read", "review.actions", "view.dashboard", "view.human_review", "view.work_board"],
    "landing_page": "/review", "can_view_as": False, "viewing_as": None, "role_options": ROLE_OPTIONS,
}
OPEN_BUSINESS_REVIEWER = {
    "authenticated": True, "user": "open@example.test", "role": "business", "role_label": "Business",
    "roles": ["business", "reviewer"], "role_labels": ["Business", "Reviewer"],
    "permissions": ["console.read", "review.actions", "view.business", "view.dashboard", "view.human_review",
                    "view.work_board"],
    "landing_page": "/business", "can_view_as": False, "viewing_as": None, "role_options": ROLE_OPTIONS,
}
GATED_ADMINISTRATOR = {
    "authenticated": True, "user": "admin@example.test", "role": "administrator", "role_label": "Administrator",
    "roles": ["administrator"], "role_labels": ["Administrator"], "permissions": ["*"],
    "landing_page": "/cc", "can_view_as": True, "viewing_as": None, "role_options": ROLE_OPTIONS,
}
GATED_VIEW_AS_REVIEWER = {
    "authenticated": True, "user": "admin@example.test", "role": "reviewer", "role_label": "Reviewer",
    "roles": ["administrator"], "role_labels": ["Administrator"],
    "permissions": ["console.read", "review.actions", "view.dashboard", "view.human_review", "view.work_board"],
    "landing_page": "/review", "can_view_as": True, "viewing_as": "reviewer", "role_options": ROLE_OPTIONS,
}


def assert_json_response(response, status: int, expected: dict) -> None:
    """Pin the serialized body and complete response-header set for these JSON endpoints."""
    encoded = json.dumps(expected, separators=(",", ":"), ensure_ascii=False).encode()
    assert response.status_code == status
    assert response.content == encoded
    assert dict(response.headers) == {
        "referrer-policy": "same-origin",
        "content-length": str(len(encoded)),
        "content-type": "application/json",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
    }


def signed(subject: str, roles: str | tuple[str, ...], secret: str = SECRET, *, view_as: str | None = None) -> str:
    return issue_session(subject, roles, EXPIRES, secret, view_as=view_as)


def test_open_session_response_goldens_are_pinned_before_the_move(ctx):
    ctx.settings.console_login = False
    ctx.settings.console_session_secret = SECRET
    client = TestClient(create_app(ctx))

    assert_json_response(client.get("/v1/console/session"), 200, OPEN_ANONYMOUS)

    client.cookies.set("aiflow4_session", signed("person@example.test", "reviewer"))
    # Forged identity headers cannot replace the signed cookie identity or grant administrator privileges.
    assert_json_response(client.get("/v1/console/session", headers={
        "X-AIFLOW4-Role": "administrator", "X-AIFLOW4-User": "forged@example.test",
    }), 200, OPEN_REVIEWER)


def test_gated_session_response_goldens_cover_assigned_effective_and_view_as_roles(ctx):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SECRET
    client = TestClient(create_app(ctx))

    client.cookies.set("aiflow4_session", signed("admin@example.test", "administrator"))
    assert_json_response(client.get("/v1/console/session"), 200, GATED_ADMINISTRATOR)

    client.cookies.set("aiflow4_session", signed("admin@example.test", "administrator", view_as="reviewer"))
    assert_json_response(client.get("/v1/console/session"), 200, GATED_VIEW_AS_REVIEWER)


def test_session_route_is_discovered_once_and_keeps_its_handler(ctx):
    app = create_app(ctx)
    matching = [route for route in concrete_routes(app.routes)
                if route.path == "/v1/console/session" and "GET" in route.methods]

    assert len(matching) == 1
    assert matching[0].name == "console_session"
    assert matching[0].endpoint.__module__ == "aiflow.v4.control.routes.console_session"
    assert matching[0].response_model is dict


@pytest.mark.parametrize("case", ("malformed", "disabled", "deleted"))
def test_open_session_authentication_failures_keep_pre_move_responses(ctx, case):
    ctx.settings.console_login = False
    ctx.settings.console_session_secret = SECRET
    if case == "disabled":
        login.seed_user(ctx.db, "disabled@example.test", "reviewer")
        ctx.db.run("UPDATE console_users SET disabled=true WHERE email=%s", ("disabled@example.test",))
    token = {
        "malformed": "forged.cookie.value",
        "disabled": signed("disabled@example.test", "reviewer"),
        # An absent console_users row remains a supported stateless signed session.
        "deleted": signed("person@example.test", "reviewer"),
    }[case]
    client = TestClient(create_app(ctx))
    client.cookies.set("aiflow4_session", token)

    expected = OPEN_REVIEWER if case == "deleted" else OPEN_ANONYMOUS
    assert_json_response(client.get("/v1/console/session"), 200, expected)


@pytest.mark.parametrize("case,status,body", (
    ("missing", 401, {"detail": "sign in required"}),
    ("malformed", 401, {"detail": "not signed in"}),
    ("expired", 401, {"detail": "not signed in"}),
    ("disabled", 401, {"detail": "not signed in"}),
))
def test_gated_session_authentication_failures_keep_pre_move_responses(ctx, case, status, body):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SECRET
    if case == "disabled":
        login.seed_user(ctx.db, "disabled@example.test", "reviewer")
        ctx.db.run("UPDATE console_users SET disabled=true WHERE email=%s", ("disabled@example.test",))
    client = TestClient(create_app(ctx))
    if case != "missing":
        token = {
            "malformed": "forged.cookie.value",
            "expired": signed("person@example.test", "reviewer"),
            "disabled": signed("disabled@example.test", "reviewer"),
        }[case]
        if case == "expired":
            token = issue_session("person@example.test", "reviewer", 1, SECRET)
        client.cookies.set("aiflow4_session", token)

    assert_json_response(client.get("/v1/console/session", headers={
        "X-AIFLOW4-Role": "administrator", "X-AIFLOW4-User": "forged@example.test",
    }), status, body)


@pytest.mark.parametrize("secret", ("", "short"))
@pytest.mark.parametrize("has_cookie", (False, True))
def test_gated_session_checks_cookie_presence_with_unconfigured_or_short_keys(ctx, secret, has_cookie):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = secret
    client = TestClient(create_app(ctx))
    if has_cookie:
        client.cookies.set("aiflow4_session", signed("person@example.test", "reviewer"))

    expected = {"detail": "not signed in" if has_cookie else "sign in required"}
    assert_json_response(client.get("/v1/console/session"), 401, expected)


def test_parallel_app_contexts_keep_distinct_signed_identities_and_login_settings(ctx):
    open_ctx = copy(ctx)
    open_ctx.settings = copy(ctx.settings)
    open_ctx.settings.console_login = False
    open_ctx.settings.console_session_secret = SECRET
    gated_ctx = copy(ctx)
    gated_ctx.settings = copy(ctx.settings)
    gated_ctx.settings.console_login = True
    gated_ctx.settings.console_session_secret = OTHER_SECRET
    open_client = TestClient(create_app(open_ctx))
    gated_client = TestClient(create_app(gated_ctx))
    open_client.cookies.set("aiflow4_session", signed("open@example.test", ("business", "reviewer"), SECRET))
    gated_client.cookies.set("aiflow4_session", signed("gated@example.test", "reviewer", OTHER_SECRET))
    forged = {"X-AIFLOW4-Role": "administrator", "X-AIFLOW4-User": "forged@example.test"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        open_response, gated_response = pool.map(lambda client: client.get("/v1/console/session", headers=forged),
                                                 (open_client, gated_client))

    expected_open = {**OPEN_BUSINESS_REVIEWER, "user": "open@example.test"}
    expected_gated = {**OPEN_REVIEWER, "user": "gated@example.test"}
    assert_json_response(open_response, 200, expected_open)
    assert_json_response(gated_response, 200, expected_gated)


def test_session_lookup_failure_returns_original_500_without_cookie_write_or_event(ctx, monkeypatch):
    ctx.settings.console_login = False
    ctx.settings.console_session_secret = SECRET
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    token = signed("person@example.test", "reviewer")
    client.cookies.set("aiflow4_session", token)
    before_writes = ctx.db.one("SELECT count(*) AS count FROM write_audit")["count"]
    before_events = ctx.db.one("SELECT count(*) AS count FROM events")["count"]
    lookup_calls = []

    def fail_lookup(sql, *_args, **_kwargs):
        lookup_calls.append(sql)
        assert sql.lstrip().upper().startswith("SELECT")
        raise RuntimeError("session lookup failed")

    def fail_write(*_args, **_kwargs):
        pytest.fail("session lookup failure attempted a database write")

    def fail_event(*_args, **_kwargs):
        pytest.fail("session lookup failure emitted an event")

    monkeypatch.setattr(ctx.db, "one", fail_lookup)
    monkeypatch.setattr(ctx.db, "run", fail_write)
    monkeypatch.setattr(ctx.db, "transaction", fail_write)
    monkeypatch.setattr(ctx.events, "emit", fail_event)
    response = client.get("/v1/console/session")
    monkeypatch.undo()

    assert lookup_calls
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert dict(response.headers) == {
        "content-length": str(len(b"Internal Server Error")),
        "content-type": "text/plain; charset=utf-8",
    }
    assert "set-cookie" not in response.headers
    assert client.cookies.get("aiflow4_session") == token
    assert ctx.db.one("SELECT count(*) AS count FROM write_audit")["count"] == before_writes
    assert ctx.db.one("SELECT count(*) AS count FROM events")["count"] == before_events
