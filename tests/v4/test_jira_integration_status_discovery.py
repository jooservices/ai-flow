"""The Jira status read is discovered per app and still passes the console sign-in gate."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiflow.v4.control import jira_connect
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.route_registry import concrete_routes
from aiflow.v4.control.routes import jira_integration_status

SESSION_SECRET = "jira-status-session-secret-that-is-long-enough"
STATUS = {"connected": True, "source": "oauth", "state": "connected", "site_name": "Example Jira"}
JIRA_PATHS = {
    ("GET", "/v1/integrations/jira", "jira_status"),
    ("POST", "/v1/integrations/jira/oauth/start", "jira_oauth_start"),
    ("GET", "/v1/integrations/jira/oauth/callback", "jira_oauth_callback"),
    ("POST", "/v1/integrations/jira/api-token", "jira_api_token"),
    ("POST", "/v1/integrations/jira/disconnect", "jira_disconnect"),
}


class _Auth:
    def __init__(self, status=STATUS):
        self.value = status
        self.calls = 0
        self.lock = Lock()

    def status(self):
        with self.lock:
            self.calls += 1
        return dict(self.value)


def _simple_app(owner_token, auth):
    ctx = SimpleNamespace(settings=SimpleNamespace(owner_token=owner_token), jira_auth=auth)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(jira_integration_status.router)
    return app


def _mounted_jira_routes(app):
    return [route for route in concrete_routes(app.routes)
            if route.path.startswith("/v1/integrations/jira")]


def test_discovered_status_and_remaining_installer_routes_are_unique(ctx):
    app = create_app(ctx)
    routes = _mounted_jira_routes(app)
    inventory = [(method, route.path, route.name) for route in routes for method in route.methods
                 if route.path.startswith("/v1/integrations/jira")]

    assert set(inventory) == JIRA_PATHS
    assert len(inventory) == len(JIRA_PATHS)
    status_route = next(route for route in routes if route.path == "/v1/integrations/jira")
    assert status_route.endpoint.__name__ == "jira_status"
    assert status_route.endpoint.checks_bearer is True
    assert status_route.response_model is dict


def test_isolated_router_uses_each_request_app_context_and_keeps_reads_independent():
    first_auth, second_auth = _Auth({"source": "first"}), _Auth({"source": "second"})
    first, second = _simple_app("first-token", first_auth), _simple_app("second-token", second_auth)
    first_client, second_client = TestClient(first), TestClient(second)

    assert first_client.get("/v1/integrations/jira", headers={"Authorization": "Bearer first-token"}).json() == {
        "source": "first"}
    assert second_client.get("/v1/integrations/jira", headers={"Authorization": "Bearer second-token"}).json() == {
        "source": "second"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: second_client.get(
            "/v1/integrations/jira", headers={"Authorization": "Bearer second-token"}).json(), range(8)))

    assert results == [{"source": "second"}] * 8
    assert first_auth.calls == 1
    assert second_auth.calls == 9


@pytest.mark.parametrize("console_login", [False, True])
def test_assembled_app_gate_preserves_owner_session_and_denial_behavior(ctx, monkeypatch, console_login):
    auth = _Auth()
    ctx.jira_auth = auth
    ctx.settings.console_login = console_login
    ctx.settings.console_session_secret = SESSION_SECRET
    app = create_app(ctx)
    monkeypatch.setattr(jira_connect.licensing, "require_writable",
                        lambda *_args: pytest.fail("status reads must not check write licensing"))
    monkeypatch.setattr(jira_connect, "write_audit",
                        lambda *_args, **_kwargs: pytest.fail("status reads must not write audit rows"))

    with TestClient(app) as client:
        missing = client.get("/v1/integrations/jira")
        if console_login:
            assert missing.status_code == 401 and missing.json() == {"detail": "sign in required"}
        else:
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"] == "Bearer"

        wrong = client.get("/v1/integrations/jira", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401
        assert wrong.json() == {"detail": "owner authorization required"}
        assert wrong.headers["www-authenticate"] == "Bearer"
        for malformed in ("Bearer owner-secret trailing", "Basic owner-secret"):
            refused = client.get("/v1/integrations/jira", headers={"Authorization": malformed})
            assert refused.status_code == 401
            assert refused.json() == {"detail": "owner authorization required"}
            assert refused.headers["www-authenticate"] == "Bearer"

        owner = client.get("/v1/integrations/jira", headers={"Authorization": "Bearer owner-secret"})
        assert owner.status_code == 200 and owner.json() == STATUS

        allowed = issue_session("sa@example.test", "sa", 4_102_444_800, SESSION_SECRET)
        permitted = client.get("/v1/integrations/jira", headers={"Cookie": f"{COOKIE_NAME}={allowed}"})
        assert permitted.status_code == 200 and permitted.json() == STATUS

        denied = issue_session("reviewer@example.test", "reviewer", 4_102_444_800, SESSION_SECRET)
        refused = client.get("/v1/integrations/jira", headers={"Cookie": f"{COOKIE_NAME}={denied}"})
        assert refused.status_code == 403 and refused.json() == {"detail": "not available for your role"}

    assert auth.calls == 2


def test_missing_auth_is_checked_after_authorization_and_returns_existing_503(ctx):
    ctx.jira_auth = None
    ctx.settings.console_login = False
    app = create_app(ctx)

    with TestClient(app) as client:
        unauthorized = client.get("/v1/integrations/jira")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        unavailable = client.get("/v1/integrations/jira", headers={"Authorization": "Bearer owner-secret"})

    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Jira connect is not configured"}
