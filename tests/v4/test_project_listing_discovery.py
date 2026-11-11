"""Characterize the managed project listing before its route moves to discovery."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control import api, login
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.features.projects import service as project_service
from aiflow.v4.control.route_registry import concrete_routes

SESSION_SECRET = "project-listing-session-secret-has-32-bytes"
SESSION_EXPIRY = 2_000_000_000
PROJECT_ROWS = [
    {"key": "ALPHA", "repo": "example/alpha"},
    {"key": "BETA", "repo": "example/beta"},
    {"key": "ZETA", "repo": "example/zeta"},
]
INVALID_PROJECTS = {
    "ZETA": "invalid zeta",
    "ALPHA": "invalid alpha",
    "BETA": "invalid beta",
}


def _listing_inputs(ctx, monkeypatch, rows=PROJECT_ROWS, invalid_projects=INVALID_PROJECTS):
    repo = SimpleNamespace(invalid_projects=invalid_projects)
    ctx.repo = lambda: repo
    monkeypatch.setattr(project_service, "managed_project_list", lambda _ctx, _repo: rows)
    return repo


def _signed_in(client, ctx, email: str, role: str = "owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session(email, role, SESSION_EXPIRY, SESSION_SECRET))


def _role_client(ctx, enabled: bool = True) -> TestClient:
    ctx.settings.console_login = enabled
    ctx.settings.console_session_secret = SESSION_SECRET
    return TestClient(api.create_app(ctx))


def test_open_listing_returns_sorted_rows_and_invalid_projects_and_handles_empty_registry(client, ctx, monkeypatch):
    ctx.settings.console_login = False
    _listing_inputs(ctx, monkeypatch)

    response = client.get("/v1/projects")

    assert response.status_code == 200
    assert [row["key"] for row in response.json()["projects"]] == ["ALPHA", "BETA", "ZETA"]
    assert response.json()["invalid_projects"] == [
        {"file": "ALPHA.yaml", "reason": "invalid alpha"},
        {"file": "BETA.yaml", "reason": "invalid beta"},
        {"file": "ZETA.yaml", "reason": "invalid zeta"},
    ]

    _listing_inputs(ctx, monkeypatch, rows=[], invalid_projects={})
    empty = client.get("/v1/projects")
    assert empty.status_code == 200 and empty.json() == {"projects": [], "invalid_projects": []}


def test_listing_filters_both_lists_for_restricted_optional_sessions_and_keeps_unrestricted_rows(
    client, ctx, monkeypatch
):
    _listing_inputs(ctx, monkeypatch)
    ctx.settings.console_login = False
    ctx.settings.console_session_secret = SESSION_SECRET
    login.seed_user(ctx.db, "limited-list@example.test", "owner")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["BETA"], "limited-list@example.test"))
    _signed_in(client, ctx, "limited-list@example.test")

    restricted = client.get("/v1/projects")
    assert restricted.status_code == 200
    assert [row["key"] for row in restricted.json()["projects"]] == ["BETA"]
    assert restricted.json()["invalid_projects"] == [{"file": "BETA.yaml", "reason": "invalid beta"}]

    login.seed_user(ctx.db, "unrestricted-list@example.test", "owner")
    _signed_in(client, ctx, "unrestricted-list@example.test")
    unrestricted = client.get("/v1/projects")
    assert [row["key"] for row in unrestricted.json()["projects"]] == ["ALPHA", "BETA", "ZETA"]
    assert len(unrestricted.json()["invalid_projects"]) == 3


def test_login_gate_preserves_authentication_authorization_audit_and_no_disclosure(ctx, monkeypatch):
    _listing_inputs(ctx, monkeypatch)
    client = _role_client(ctx)
    anonymous = client.get("/v1/projects")
    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": "sign in required"}
    assert "ALPHA" not in anonymous.text
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='access_denied'")["n"] == 0

    login.seed_user(ctx.db, "business-list@example.test", "business")
    _signed_in(client, ctx, "business-list@example.test", "business")
    refused = client.get("/v1/projects")
    assert refused.status_code == 403
    assert refused.json() == {"detail": "not available for your role"}
    assert "ALPHA" not in refused.text
    audit = ctx.db.one("SELECT actor,accepted,result,payload FROM write_audit WHERE action='access_denied'")
    assert audit == {
        "actor": "business-list@example.test",
        "accepted": False,
        "result": "not available for your role",
        "payload": {"path": "/v1/projects", "method": "GET", "roles": ["business"]},
    }
    client.close()


def test_login_enabled_restricted_owner_gets_the_same_project_filter(ctx, monkeypatch):
    _listing_inputs(ctx, monkeypatch)
    login.seed_user(ctx.db, "scoped-list@example.test", "owner")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["ALPHA"], "scoped-list@example.test"))
    client = _role_client(ctx)
    _signed_in(client, ctx, "scoped-list@example.test")

    response = client.get("/v1/projects")

    assert response.status_code == 200
    assert [row["key"] for row in response.json()["projects"]] == ["ALPHA"]
    assert response.json()["invalid_projects"] == [{"file": "ALPHA.yaml", "reason": "invalid alpha"}]
    client.close()


def test_listing_repository_and_service_failures_propagate_without_translation(client, ctx, monkeypatch):
    ctx.settings.console_login = False

    def repository_failure():
        raise RuntimeError("repository unavailable")

    ctx.repo = repository_failure
    with pytest.raises(RuntimeError, match="repository unavailable"):
        client.get("/v1/projects")

    _listing_inputs(ctx, monkeypatch)

    def service_failure(_ctx, _repo):
        raise ValueError("listing unavailable")

    monkeypatch.setattr(project_service, "managed_project_list", service_failure)
    with pytest.raises(ValueError, match="listing unavailable"):
        client.get("/v1/projects")


def test_assembled_app_has_one_listing_route_and_keeps_the_catalog_route(ctx):
    routes = list(concrete_routes(api.create_app(ctx).routes))

    assert sum("GET" in (route.methods or set()) and route.path == "/v1/projects" for route in routes) == 1
    assert sum("GET" in (route.methods or set()) and route.path == "/v1/projects/catalog" for route in routes) == 1


def test_listing_is_reexported_from_the_discovered_route_package(ctx):
    from aiflow.v4.control.features.projects import discovered_listing
    from aiflow.v4.control.routes.project_listing import router as exported_router

    routes = list(concrete_routes(api.create_app(ctx).routes))
    listing_routes = [route for route in routes if "GET" in (route.methods or set())
                     and route.path == "/v1/projects"]

    assert exported_router is discovered_listing.router
    assert len(listing_routes) == 1
    assert listing_routes[0].endpoint.__module__ == discovered_listing.__name__
