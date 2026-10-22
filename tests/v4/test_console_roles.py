"""Role-based console views, project scopes, administration and legacy role migration."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import (CANONICAL_ROLES, ROLE_LABELS, ROLE_PERMISSIONS,
                                             has_permission, issue_session, landing_page, normalize_roles)
from aiflow.v4.core.controlrepo import Project
from conftest import add_ticket


SESSION_SECRET = "test-console-role-secret-with-at-least-32-bytes"
SESSION_EXPIRY = 2_000_000_000

ROLE_VIEWS = {
    "administrator": {"cc", "board", "delivery", "sa", "lead", "business", "review", "stakeholder",
                      "machines", "knowledge", "integrations", "projects", "users"},
    "product_owner": {"cc", "board", "delivery", "business", "lead", "stakeholder", "review", "knowledge"},
    "delivery": {"cc", "board", "delivery", "review"},
    "sa": {"cc", "board", "sa", "machines", "integrations", "knowledge"},
    "ba": {"cc", "board", "business", "knowledge"},
    "team_lead": {"cc", "board", "delivery", "lead", "review", "knowledge"},
    "business": {"cc", "board", "business"},
    "reviewer": {"cc", "board", "review"},
    "stakeholder": {"cc", "board", "stakeholder"},
}
VIEW_PATHS = {"cc": "/cc", "board": "/board", "delivery": "/delivery", "sa": "/sa", "lead": "/lead",
              "business": "/business", "review": "/review", "stakeholder": "/stakeholder",
              "machines": "/machines", "knowledge": "/knowledge", "integrations": "/integrations", "projects": "/projects",
              "users": "/users"}
VIEW_PERMISSIONS = {"cc": "view.dashboard", "board": "view.work_board", "delivery": "view.delivery",
                    "sa": "view.sa", "lead": "view.team_lead", "business": "view.business",
                    "review": "view.human_review", "stakeholder": "view.stakeholder",
                    "machines": "view.machines_ai", "knowledge": "view.knowledge", "integrations": "view.integrations",
                    "projects": "projects.manage", "users": "console.manage"}
ALLOWED_API = {
    "administrator": "/v1/console/users",
    "product_owner": "/v1/business/TESTWP/change-requests",
    "delivery": "/v1/reviews",
    "sa": "/v1/nodes/cc/operations",
    "ba": "/v1/knowledge/resources",
    "team_lead": "/v1/knowledge/resources",
    "business": "/v1/business/TESTWP/change-requests",
    "reviewer": "/v1/reviews",
    "stakeholder": "/v1/stakeholder/TESTWP",
}
API_VIEW_REQUESTS = [
    ("console.manage", "/v1/console/users"),
    ("view.integrations", "/v1/integrations/github"),
    ("view.knowledge", "/v1/knowledge/resources"),
    ("view.human_review", "/v1/reviews"),
    ("view.business", "/v1/business/TESTWP/change-requests"),
    ("view.stakeholder", "/v1/stakeholder/TESTWP"),
    ("projects.manage", "/v1/projects"),
    ("view.machines_ai", "/v1/nodes/cc/operations"),
]


def role_client(ctx, role: str | list[str] | None = None, subject: str = "person@example.test") -> TestClient:
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SESSION_SECRET
    app = create_app(ctx)
    from aiflow.v4.control.ui import mount_ui
    mount_ui(app, ctx)
    app.state.sync_accounts()
    client = TestClient(app)
    if role is not None:
        client.cookies.set("aiflow4_session", issue_session(subject, role, SESSION_EXPIRY, SESSION_SECRET))
    return client


def test_role_permissions_cover_the_ticket_role_view_table():
    assert set(CANONICAL_ROLES) == set(ROLE_VIEWS)
    for role, views in ROLE_VIEWS.items():
        permissions = ROLE_PERMISSIONS[role]
        expected = {VIEW_PERMISSIONS[view] for view in views}
        assert expected.issubset(permissions) or "*" in permissions
        assert has_permission(role, "view.dashboard") and has_permission(role, "view.work_board")
    assert ROLE_LABELS["super_admin"] == "Administrator"
    assert normalize_roles(["owner", "lead", "owner"]) == ("administrator", "team_lead")


def test_every_role_can_open_its_views_and_is_refused_for_other_views_and_apis(ctx):
    client = role_client(ctx)
    for role, expected_views in ROLE_VIEWS.items():
        subject = role + "@example.test"
        client.cookies.set("aiflow4_session", issue_session(subject, role, SESSION_EXPIRY, SESSION_SECRET))
        session = client.get("/v1/console/session")
        assert session.status_code == 200
        assert session.json()["role"] == role
        assert session.json()["permissions"] == sorted(ROLE_PERMISSIONS[role])
        for view, path in VIEW_PATHS.items():
            response = client.get(path)
            if view in expected_views:
                assert response.status_code == 200, (role, path, response.text[:200])
            else:
                assert response.status_code == 403, (role, path, response.status_code)
                assert "not available for your role" in response.text
        allowed = client.get(ALLOWED_API[role])
        assert allowed.status_code != 403, (role, allowed.text)
        if role != "administrator":
            missing = next((permission, path) for permission, path in API_VIEW_REQUESTS
                           if permission not in ROLE_PERMISSIONS[role])
            refused = client.get(missing[1])
            assert refused.status_code == 403, (role, missing, refused.status_code)
            assert refused.json()["detail"] == "not available for your role"
            audit = ctx.db.one("SELECT actor,accepted,result,payload FROM write_audit WHERE target='console'"
                               " AND action='access_denied' AND actor=%s ORDER BY id DESC LIMIT 1", (subject,))
            assert audit and not audit["accepted"] and audit["result"] == "not available for your role"
            assert audit["payload"]["path"] == missing[1]


def test_multi_role_landing_priority_matches_the_ticket():
    assert landing_page(["stakeholder", "reviewer"]) == "/review"
    assert landing_page(["business", "ba"]) == "/business"
    assert landing_page(["sa", "ba"]) == "/sa"
    assert landing_page(["delivery", "product_owner"]) == "/cc"
    assert landing_page(["team_lead", "reviewer"]) == "/lead"
    assert landing_page(["business", "administrator"]) == "/cc"

def test_login_returns_the_multi_role_landing_page_and_unions_permissions(ctx):
    password = login.seed_user(ctx.db, "multi@example.test", ["business", "reviewer", "ba"])
    client = role_client(ctx)
    response = client.post("/v1/auth/login", json={"email": "multi@example.test", "password": password})
    assert response.status_code == 200
    assert response.json()["roles"] == ["business", "reviewer", "ba"]
    assert response.json()["landing_page"] == "/business"
    session = client.get("/v1/console/session").json()
    assert set(ROLE_PERMISSIONS["business"] | ROLE_PERMISSIONS["reviewer"] | ROLE_PERMISSIONS["ba"]) == set(
        session["permissions"])


def test_project_restriction_filters_dashboard_and_refuses_other_project_apis(ctx):
    ctx.repo().projects["OTHER"] = Project("OTHER", "jooservices/other")
    ctx.db.run("INSERT INTO tickets (key,project,status,type,labels,summary) VALUES"
               " ('TESTWP-1','TESTWP','To Do','Story',%s,'Visible ticket'),"
               " ('OTHER-1','OTHER','To Do','Story',%s,'Hidden ticket')", (["aiflow-ready"], ["aiflow-ready"]))
    login.seed_user(ctx.db, "limited@example.test", "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (["TESTWP"], "limited@example.test"))
    client = role_client(ctx, "business", "limited@example.test")

    snapshot = client.get("/v1/state")
    assert snapshot.status_code == 200
    assert [project["key"] for project in snapshot.json()["projects"]] == ["TESTWP"]
    assert {ticket["key"] for ticket in snapshot.json()["tickets"]} == {"TESTWP-1"}
    assert snapshot.json()["nodes"] == [] and snapshot.json()["accounts"] == []
    assert client.get("/v1/analytics/daily?project=TESTWP").status_code == 200
    for path in ("/v1/business/OTHER/release-readiness", "/v1/tickets/OTHER-1/timeline",
                 "/v1/analytics/daily?project=OTHER"):
        refused = client.get(path)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "not available for your role"
    assert client.get("/v1/tickets/TESTWP-1/timeline").status_code != 403
    denied = ctx.db.all("SELECT actor,payload FROM write_audit WHERE action='access_denied' AND actor=%s",
                        ("limited@example.test",))
    assert {row["payload"]["path"] for row in denied} >= {
        "/v1/business/OTHER/release-readiness", "/v1/tickets/OTHER-1/timeline", "/v1/analytics/daily"}


def test_project_restriction_scopes_human_review_queue_metrics_and_export(ctx):
    ctx.repo().projects["OTHER"] = Project("OTHER", "jooservices/other")
    add_ticket(ctx.db, "TESTWP-96", status="Human review", labels=("require-human",))
    add_ticket(ctx.db, "OTHER-1", status="Human review", labels=("require-human",))
    login.seed_user(ctx.db, "review-limited@example.test", "reviewer")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["TESTWP"], "review-limited@example.test"))
    client = role_client(ctx, "reviewer", "review-limited@example.test")

    queue = client.get("/v1/reviews")
    assert queue.status_code == 200
    assert [ticket["key"] for ticket in queue.json()["tickets"]] == ["TESTWP-96"]
    assert queue.json()["projects"] == ["TESTWP"]
    assert queue.json()["metrics"]["people_needed_this_week"] == 1
    exported = client.get("/v1/reviews/export.csv")
    assert exported.status_code == 200 and ",all" in exported.text
    assert "OTHER" not in exported.text

    for path in ("/v1/reviews?project=OTHER", "/v1/reviews/export.csv?project=OTHER"):
        refused = client.get(path)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "not available for your role"
    denied = ctx.db.all("SELECT actor,payload FROM write_audit WHERE action='access_denied' AND actor=%s",
                        ("review-limited@example.test",))
    assert {row["payload"]["path"] for row in denied} >= {"/v1/reviews", "/v1/reviews/export.csv"}


def test_administrator_can_add_edit_disable_and_audit_people_and_old_role_names(ctx):
    login.seed_user(ctx.db, "admin@example.test", "super_admin", "Site Admin")
    client = role_client(ctx, "super_admin", "admin@example.test")
    added = client.post("/v1/console/users", json={"email": "new@example.test", "display_name": "New Person",
                                                  "roles": ["lead"], "project_keys": ["TESTWP"]})
    assert added.status_code == 200
    temporary_password = added.json()["temporary_password"]
    row = ctx.db.one("SELECT role,roles,project_keys,password_hash FROM console_users WHERE email=%s",
                     ("new@example.test",))
    assert row["role"] == "team_lead" and row["roles"] == ["team_lead"]
    assert row["project_keys"] == ["TESTWP"] and temporary_password not in row["password_hash"]

    updated = client.put("/v1/console/users/new@example.test",
                         json={"roles": ["ba", "business"], "project_keys": []})
    assert updated.status_code == 200
    row = ctx.db.one("SELECT role,roles,project_keys FROM console_users WHERE email=%s", ("new@example.test",))
    assert row == {"role": "ba", "roles": ["ba", "business"], "project_keys": []}
    disabled = client.patch("/v1/console/users/new@example.test/disabled", json={"disabled": True})
    assert disabled.status_code == 200
    assert client.post("/v1/auth/login", json={"email": "new@example.test", "password": temporary_password}).status_code == 401

    changes = ctx.db.all("SELECT action,actor,payload FROM write_audit WHERE target='console'"
                         " AND action IN ('user_add','user_update','user_disable') ORDER BY id")
    assert [(row["action"], row["actor"]) for row in changes] == [
        ("user_add", "admin@example.test"), ("user_update", "admin@example.test"),
        ("user_disable", "admin@example.test")]
    listed = client.get("/v1/console/users").json()
    assert any(item["email"] == "new@example.test" and item["disabled"] for item in listed["users"])
    assert len(listed["audit"]) >= 3


def test_last_active_administrator_cannot_lose_role_or_be_disabled(ctx):
    login.seed_user(ctx.db, "first-admin@example.test", "administrator")
    client = role_client(ctx, "administrator", "first-admin@example.test")
    email = "first-admin@example.test"
    removed = client.put(f"/v1/console/users/{email}", json={"roles": ["business"], "project_keys": None})
    assert removed.status_code == 409 and "last administrator" in removed.json()["detail"]
    disabled = client.patch(f"/v1/console/users/{email}/disabled", json={"disabled": True})
    assert disabled.status_code == 409 and "last administrator" in disabled.json()["detail"]

    added = client.post("/v1/console/users", json={"email": "second-admin@example.test", "roles": ["administrator"]})
    assert added.status_code == 200
    assert client.put(f"/v1/console/users/{email}", json={"roles": ["business"], "project_keys": None}).status_code == 200
    client.cookies.set("aiflow4_session", issue_session("second-admin@example.test", "administrator",
                                                       SESSION_EXPIRY, SESSION_SECRET))
    last_admin = client.patch("/v1/console/users/second-admin@example.test/disabled", json={"disabled": True})
    assert last_admin.status_code == 409


def test_administrator_view_as_is_signed_and_server_enforced_without_sign_out(ctx):
    login.seed_user(ctx.db, "view-admin@example.test", "administrator")
    client = role_client(ctx, "administrator", "view-admin@example.test")
    viewed = client.post("/v1/console/view-as", json={"role": "reviewer"})
    assert viewed.status_code == 200 and viewed.json()["viewing_as"] == "reviewer"
    session = client.get("/v1/console/session").json()
    assert session["role"] == "reviewer" and session["viewing_as"] == "reviewer" and session["can_view_as"]
    assert session["permissions"] == sorted(ROLE_PERMISSIONS["reviewer"])
    refused = client.get("/v1/console/users")
    assert refused.status_code == 403 and refused.json()["detail"] == "not available for your role"
    assert client.get("/review").status_code == 200 and client.get("/integrations").status_code == 403
    restored = client.post("/v1/console/view-as/stop", json={})
    assert restored.status_code == 200
    session = client.get("/v1/console/session").json()
    assert session["role"] == "administrator" and session["permissions"] == ["*"]
    audits = ctx.db.all("SELECT actor,action,payload FROM write_audit WHERE target='console' AND action LIKE 'view_as%'"
                        " ORDER BY id")
    assert [row["action"] for row in audits] == ["view_as", "view_as_stop"]
    assert all(row["actor"] == "view-admin@example.test" for row in audits)


def test_legacy_role_migration_maps_existing_owner_lead_and_super_admin_rows(db):
    migration = Path(__file__).resolve().parents[2] / "aiflow/v4/control/migrations/011_console_role_views.sql"
    with db.pool.connection() as connection:
        connection.execute("CREATE TEMP TABLE console_users (email text PRIMARY KEY, role text NOT NULL)")
        connection.execute("INSERT INTO console_users(email,role) VALUES"
                           " ('owner@example.test','owner'),('lead@example.test','lead'),"
                           " ('install@example.test','super_admin')")
        connection.execute(migration.read_text(encoding="utf-8"))
        rows = connection.execute("SELECT email,role,roles FROM console_users ORDER BY email").fetchall()
        connection.execute("DROP TABLE console_users")
    assert rows == [
        {"email": "install@example.test", "role": "administrator", "roles": ["administrator"]},
        {"email": "lead@example.test", "role": "team_lead", "roles": ["team_lead"]},
        {"email": "owner@example.test", "role": "administrator", "roles": ["administrator"]},
    ]
