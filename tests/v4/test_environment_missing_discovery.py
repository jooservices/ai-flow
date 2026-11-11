"""Behavior and registration parity for the discovered missing-environment-items route."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aiflow.v4.control import environment as environment_reports, login
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.route_registry import concrete_routes


SESSION_SECRET = "environment-missing-test-session-secret"
SESSION_EXPIRY = 2_000_000_000
REPORTS = [
    {"ticket": "TESTWP-1", "project": "TESTWP", "state": "waiting"},
    {"ticket": "OTHER-1", "project": "OTHER", "state": "provided"},
]


def test_no_reports_returns_empty_list_and_calls_reports_once(client, ctx, monkeypatch):
    ctx.settings.console_login = False
    reports = Mock(return_value=[])
    monkeypatch.setattr(environment_reports, "reports", reports)

    response = client.get("/v1/environment/missing")

    assert response.status_code == 200
    assert response.json() == {"missing": []}
    reports.assert_called_once_with(ctx)


@pytest.mark.parametrize(
    ("project_keys", "expected"),
    [(["TESTWP"], [REPORTS[0]]), (None, REPORTS), ([], [])],
)
def test_optional_session_project_scope_filters_after_reports(
    client, ctx, monkeypatch, project_keys, expected
):
    subject = "environment-scope@example.test"
    ctx.settings.console_login = False
    ctx.settings.console_session_secret = SESSION_SECRET
    login.seed_user(ctx.db, subject, "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (project_keys, subject))
    client.cookies.set(COOKIE_NAME, issue_session(subject, "business", SESSION_EXPIRY, SESSION_SECRET))
    reports = Mock(return_value=REPORTS)
    monkeypatch.setattr(environment_reports, "reports", reports)

    response = client.get("/v1/environment/missing")

    assert response.status_code == 200
    assert response.json() == {"missing": expected}
    reports.assert_called_once_with(ctx)


def test_login_mode_preserves_missing_expired_signing_key_and_permission_errors(client, ctx):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SESSION_SECRET

    missing = client.get("/v1/environment/missing")
    assert missing.status_code == 401
    assert missing.json()["detail"] == "authenticated console session required"

    client.cookies.set(COOKIE_NAME, issue_session("person@example.test", "administrator", 1, SESSION_SECRET))
    expired = client.get("/v1/environment/missing")
    assert expired.status_code == 401
    assert expired.json()["detail"] == "authenticated console session required"

    client.cookies.clear()
    ctx.settings.console_session_secret = ""
    no_signing_key = client.get("/v1/environment/missing")
    assert no_signing_key.status_code == 503
    assert no_signing_key.json()["detail"] == "console session authentication is not configured"

    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set(COOKIE_NAME, issue_session("reader@example.test", "business", SESSION_EXPIRY, SESSION_SECRET))
    unauthorized = client.get("/v1/environment/missing")
    assert unauthorized.status_code == 403
    assert unauthorized.json()["detail"] == "not available for your role"


def test_login_enabled_authorized_session_filters_reports_by_project_scope(monkeypatch):
    from aiflow.v4.control import route_deps
    from aiflow.v4.control.features.projects.discovered_environment_missing import environment_missing_items

    ctx = SimpleNamespace(settings=SimpleNamespace(console_login=True))
    request = object()
    user = SimpleNamespace(project_keys=("TESTWP",))
    monkeypatch.setattr(route_deps, "context", lambda _request: ctx)
    console_user = Mock(return_value=user)
    monkeypatch.setattr(route_deps, "console_user_for", console_user)
    reports = Mock(return_value=REPORTS)
    monkeypatch.setattr(environment_reports, "reports", reports)

    response = environment_missing_items(request)

    assert response == {"missing": [REPORTS[0]]}
    console_user.assert_called_once_with(request, "projects.manage")
    reports.assert_called_once_with(ctx)


def test_login_disabled_with_absent_or_invalid_session_keeps_unrestricted_optional_behavior(
    client, ctx, monkeypatch
):
    ctx.settings.console_login = False
    rows = Mock(return_value=REPORTS)
    monkeypatch.setattr(environment_reports, "reports", rows)

    absent = client.get("/v1/environment/missing")
    client.cookies.set(COOKIE_NAME, "not-a-valid-session")
    invalid = client.get("/v1/environment/missing")

    assert absent.status_code == invalid.status_code == 200
    assert absent.json() == invalid.json() == {"missing": REPORTS}
    assert rows.call_count == 2
    rows.assert_called_with(ctx)


def test_reports_failure_propagates_without_being_caught(client, ctx, monkeypatch):
    ctx.settings.console_login = False
    reports = Mock(side_effect=RuntimeError("report backend failed"))
    monkeypatch.setattr(environment_reports, "reports", reports)

    with pytest.raises(RuntimeError, match="report backend failed"):
        client.get("/v1/environment/missing")

    reports.assert_called_once_with(ctx)


def test_missing_route_is_registered_once_and_neighboring_installer_routes_remain(client):
    routes = [route for route in concrete_routes(client.app.routes) if hasattr(route, "methods")]
    missing = [route for route in routes if route.path == "/v1/environment/missing"
               and "GET" in route.methods]
    installed = {(method, route.path) for route in routes for method in route.methods}

    assert len(missing) == 1
    assert ("GET", "/v1/environment/scan") in installed
    assert ("POST", "/v1/environment/scan") in installed
    assert ("POST", "/v1/environment/removals") in installed
