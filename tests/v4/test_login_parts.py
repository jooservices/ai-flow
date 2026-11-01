"""Characterize the pure login installer decisions before splitting its nested helpers."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Header
from starlette.requests import Request

from aiflow.v4.control.login_gate import (api_permission, checks_token, login_redirect_target, project_for_path,
                                          route_checks_token)
from aiflow.v4.control.login_signin import rate_limit_digest, rate_limit_key


class _Database:
    def __init__(self):
        self.queries = []

    def one(self, sql, params=()):
        self.queries.append((sql, params))
        if "SELECT project FROM tickets" in sql:
            return {"project": "DB-PROJECT"}
        return None


def _request(path: str, query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(),
                    "headers": [], "server": ("example.test", 80), "client": ("127.0.0.1", 80),
                    "scheme": "http", "http_version": "1.1", "root_path": ""})


def test_token_route_detection_reads_bearer_parameters_from_fastapi_routes():
    app = FastAPI()

    @app.get("/login-parts-machine")
    def machine_route(authorization: str = Header(default="")):
        return {"authorization": bool(authorization)}

    def machine_dependency(authorization: str = Header(default="")):
        return authorization

    @app.get("/login-parts-nested")
    def nested_machine_route(token=Depends(machine_dependency)):
        return {"authorization": bool(token)}

    @app.get("/login-parts-owner")
    def owner_route():
        return {"owner": True}

    owner_route.checks_bearer = True
    machine = next(route for route in app.routes if getattr(route, "path", "") == "/login-parts-machine")
    nested = next(route for route in app.routes if getattr(route, "path", "") == "/login-parts-nested")
    owner = next(route for route in app.routes if getattr(route, "path", "") == "/login-parts-owner")
    assert route_checks_token(machine) is True
    assert route_checks_token(nested) is True
    assert route_checks_token(owner) is True
    assert checks_token(app, {}, _request("/login-parts-machine")) is True
    assert checks_token(app, {}, _request("/login-parts-nested")) is True
    assert checks_token(app, {}, _request("/login-parts-owner")) is True
    assert checks_token(app, {}, _request("/login-parts-ordinary")) is False


def test_api_permission_maps_current_console_paths():
    cases = {
        "/v1/system-check": "node.operations.read",
        "/v1/support-bundle": "node.operations.read",
        "/v1/master/report": "view.machines_ai",
        "/v1/role-health/settings": "console.manage",
        "/v1/master/autonomy/budget": "console.manage",
        "/v1/provider-routing/decisions/1": "view.machines_ai",
        "/v1/provider-routing/settings": "console.manage",
        "/v1/console/users": "console.manage",
        "/v1/runtime-settings/notifications": "console.manage",
        "/v1/quota-refresh/now": "console.manage",
        "/v1/runs/123/inbox": "review.actions",
        "/v1/knowledge/search": "view.knowledge",
        "/v1/integrations/github": "view.integrations",
        "/v1/reviews": "view.human_review",
        "/v1/business/PROJECT": "view.business",
        "/v1/stakeholder/PROJECT": "view.stakeholder",
        "/v1/nodes/worker-1": "view.machines_ai",
        "/v1/services": "view.machines_ai",
        "/v1/logs": "view.logs",
        "/v1/log-sources": "view.logs",
        "/v1/projects/PROJECT/study-cases": "view.knowledge",
        "/v1/projects/PROJECT/report": "view.delivery",
        "/v1/projects/PROJECT": "projects.manage",
        "/v1/projects/PROJECT/versions": "view.delivery",
        "/v1/memory/search": "view.knowledge",
        "/v1/tickets/JAIF-1": "view.work_board",
        "/v1/analytics/usage": "view.dashboard",
        "/v1/unmapped": "view.dashboard",
    }
    assert {path: api_permission(path) for path in cases} == cases


def test_project_scope_uses_path_query_or_ticket_lookup_as_before():
    ctx = SimpleNamespace(db=_Database())

    assert project_for_path(ctx, _request("/v1/projects/PROJECT")) == "PROJECT"
    assert project_for_path(ctx, _request("/v1/business/OTHER/summary")) == "OTHER"
    assert project_for_path(ctx, _request("/v1/tickets/JAIF-9")) == "DB-PROJECT"
    assert project_for_path(ctx, _request("/v1/analytics/usage", "project=QUERY-PROJECT")) == "QUERY-PROJECT"
    assert project_for_path(ctx, _request("/v1/state")) == ""
    assert ctx.db.queries == [("SELECT project FROM tickets WHERE key=%s", ("JAIF-9",))]


def test_rate_limit_key_and_digest_are_stable():
    assert rate_limit_key("test-prefix:", "ip", "abc") == "test-prefix:console_login:ip:abc"
    assert rate_limit_digest("192.0.2.1") == hashlib.sha256(b"192.0.2.1").hexdigest()[:32]


def test_redirect_target_preserves_safe_path_and_query():
    assert login_redirect_target("/cc", "tab=security") == "/login?next=%2Fcc%3Ftab%3Dsecurity"
    assert login_redirect_target("//evil.example", "") == "/login?next=%2Fcc"
