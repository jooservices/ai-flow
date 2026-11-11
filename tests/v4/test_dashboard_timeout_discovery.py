"""The timeout analytics endpoint belongs to the discovered dashboard router."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control import api, login, route_deps
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.route_registry import concrete_routes
from aiflow.v4.control.routes import dashboard
from test_dashboard_timeouts import add_dev_run


TIMEOUT = ("GET", "/v1/dashboard/timeouts")
SILENT_RUNS = ("GET", "/v1/dashboard/silent-runs")
SESSION_SECRET = "dashboard-timeout-discovery-session-secret"
SESSION_EXPIRY = 2_000_000_000


def _matches(routes, expected):
    method, path = expected
    return [route for route in routes if method in (route.methods or ()) and route.path == path]


def test_timeout_route_is_discovered_once_and_silent_runs_stays_mounted(ctx):
    module_routes = [route for route in dashboard.router.routes
                     if "GET" in (route.methods or ()) and route.path == TIMEOUT[1]]
    app_routes = list(concrete_routes(api.create_app(ctx).routes))
    timeouts = _matches(app_routes, TIMEOUT)
    silent_runs = _matches(app_routes, SILENT_RUNS)

    assert len(module_routes) == len(timeouts) == 1
    assert timeouts[0].endpoint.__module__ == "aiflow.v4.control.routes.dashboard"
    assert len(silent_runs) == 1
    assert silent_runs[0].endpoint.__module__ == "aiflow.v4.control.dashboard_silent_routes"
    assert not hasattr(api, "dashboard_timeout_routes")


def test_timeout_route_default_window_and_query_validation(client):
    response = client.get("/v1/dashboard/timeouts")

    assert response.status_code == 200
    assert response.json() == {"days": 30, "developer_runs": 0, "timeouts": 0, "timeout_share_pct": 0.0,
                               "target_share_pct": 10.0, "daily": [], "tickets": []}
    for days in (1, 90):
        accepted = client.get("/v1/dashboard/timeouts", params={"days": days})
        assert accepted.status_code == 200 and accepted.json()["days"] == days
    for days in (0, 91, "not-an-integer", str(10**100)):
        assert client.get("/v1/dashboard/timeouts", params={"days": days}).status_code == 422


def test_http_scope_isolation_across_apps_and_login_modes(ctx):
    contexts = [replace(ctx, settings=copy.copy(ctx.settings)) for _ in range(2)]
    for app_ctx, login_enabled in zip(contexts, (False, True)):
        app_ctx.settings.console_login = login_enabled
        app_ctx.settings.console_session_secret = SESSION_SECRET
    for project in ("TESTWP", "OTHER"):
        add_dev_run(ctx.db, ticket=f"{project}-901", project=project, outcome="timeout")
    users = (("timeout-first@example.test", "TESTWP"), ("timeout-second@example.test", "OTHER"))
    for email, project in users:
        login.seed_user(ctx.db, email, "business")
        ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", ([project], email))

    apps = [api.create_app(app_ctx) for app_ctx in contexts]
    for app in apps:
        app.state.sync_accounts()
    assert all(app.state.ctx is app_ctx for app, app_ctx in zip(apps, contexts))
    clients = [TestClient(app) for app in apps]
    for client, (email, _project) in zip(clients, users):
        client.cookies.set(COOKIE_NAME, issue_session(email, "business", SESSION_EXPIRY, SESSION_SECRET))
    start = Barrier(2)

    def request(client):
        start.wait(timeout=10)
        return client.get("/v1/dashboard/timeouts", params={"days": 1})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, clients))
    assert [response.status_code for response in responses] == [200, 200]
    for response, (_email, project) in zip(responses, users):
        data = response.json()
        assert data["days"] == 1 and (data["developer_runs"], data["timeouts"]) == (1, 1)
        assert {row["project"] for row in data["tickets"]} == {project}
        assert {row["ticket"] for row in data["tickets"]} == {f"{project}-901"}

    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", ([], users[0][0]))
    empty = clients[0].get("/v1/dashboard/timeouts", params={"days": 1}).json()
    assert (empty["developer_runs"], empty["timeouts"], empty["tickets"]) == (0, 0, [])
    ctx.db.run("UPDATE console_users SET project_keys=NULL WHERE email=%s", (users[0][0],))
    unrestricted = clients[0].get("/v1/dashboard/timeouts", params={"days": 1}).json()
    assert (unrestricted["developer_runs"], unrestricted["timeouts"]) == (2, 2)
    assert {row["project"] for row in unrestricted["tickets"]} == {"TESTWP", "OTHER"}


@pytest.mark.parametrize("console_login", [True, False])
@pytest.mark.parametrize("project_keys", [None, []])
def test_handler_uses_the_matching_auth_helper_and_preserves_project_scope(
        monkeypatch, console_login, project_keys):
    db = object()
    ctx = SimpleNamespace(settings=SimpleNamespace(console_login=console_login), db=db)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))
    user = SimpleNamespace(project_keys=project_keys)
    auth_calls = []
    analytics_calls = []
    expected = {"result": "unchanged"}

    monkeypatch.setattr(route_deps, "console_user_for",
                        lambda received, permission: auth_calls.append(("required", received, permission)) or user)
    monkeypatch.setattr(route_deps, "optional_console_user",
                        lambda received_ctx, received: auth_calls.append(("optional", received_ctx, received)) or user)
    monkeypatch.setattr(dashboard.run_analytics, "timeouts",
                        lambda received_db, days, projects: analytics_calls.append((received_db, days, projects))
                        or expected)

    assert dashboard.dashboard_timeouts(request, days=1) is expected
    assert analytics_calls == [(db, 1, project_keys)]
    if console_login:
        assert auth_calls == [("required", request, "view.dashboard")]
    else:
        assert auth_calls == [("optional", ctx, request)]
    assert analytics_calls[0][2] is project_keys


def test_handler_propagates_analytics_failures_without_retry(monkeypatch):
    failure = RuntimeError("analytics unavailable")
    ctx = SimpleNamespace(settings=SimpleNamespace(console_login=False), db=object())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))
    calls = []
    monkeypatch.setattr(route_deps, "optional_console_user", lambda *_args: None)

    def fail(*args):
        calls.append(args)
        raise failure

    monkeypatch.setattr(dashboard.run_analytics, "timeouts", fail)
    with pytest.raises(RuntimeError) as caught:
        dashboard.dashboard_timeouts(request, days=30)

    assert caught.value is failure
    assert len(calls) == 1


def test_each_request_uses_its_own_context_and_project_scope(monkeypatch):
    first_db, second_db = object(), object()
    first_projects, second_projects = ["FIRST"], ["SECOND"]
    requests = [SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(
        settings=SimpleNamespace(console_login=True), db=db)))) for db in (first_db, second_db)]
    scopes = {id(requests[0]): first_projects, id(requests[1]): second_projects}
    calls = []
    monkeypatch.setattr(route_deps, "console_user_for",
                        lambda request, _permission: SimpleNamespace(project_keys=scopes[id(request)]))
    monkeypatch.setattr(dashboard.run_analytics, "timeouts",
                        lambda db, _days, projects: calls.append((db, projects)) or {"projects": projects})

    assert dashboard.dashboard_timeouts(requests[0], days=1) == {"projects": first_projects}
    assert dashboard.dashboard_timeouts(requests[1], days=1) == {"projects": second_projects}
    assert calls == [(first_db, first_projects), (second_db, second_projects)]
