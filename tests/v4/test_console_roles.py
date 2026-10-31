"""Role-based console views, project scopes, administration and legacy role migration."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import (CANONICAL_ROLES, ROLE_LABELS, ROLE_PERMISSIONS,
                                             has_permission, issue_session, landing_page, normalize_roles)
from aiflow.v4.control.db import J
from aiflow.v4.core.controlrepo import Project
from conftest import add_ticket, join_node
from aiflow.v4.control.ui import console_template


SESSION_SECRET = "test-console-role-secret-with-at-least-32-bytes"
SESSION_EXPIRY = 2_000_000_000

ROLE_VIEWS = {
    "administrator": {"cc", "board", "delivery", "sa", "lead", "business", "review", "stakeholder",
                      "machines", "knowledge", "integrations", "projects", "users", "support", "logs"},
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
              "users": "/users", "support": "/support", "logs": "/logs"}
VIEW_PERMISSIONS = {"cc": "view.dashboard", "board": "view.work_board", "delivery": "view.delivery",
                    "sa": "view.sa", "lead": "view.team_lead", "business": "view.business",
                    "review": "view.human_review", "stakeholder": "view.stakeholder",
                    "machines": "view.machines_ai", "knowledge": "view.knowledge", "integrations": "view.integrations",
                    "projects": "projects.manage", "users": "console.manage", "support": "console.manage",
                    "logs": "view.logs"}
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
    ("view.logs", "/v1/log-sources"),
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
        assert client.get("/dashboard").status_code == 200
        assert client.get("/work").status_code == 200
        assert client.get("/").status_code == 200


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
    assert snapshot.json()["role_health"]["roles"] == []
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


def test_system_check_and_support_bundle_require_node_operations_read(ctx):
    client = role_client(ctx)
    for role in ("reviewer", "business"):
        client.cookies.set("aiflow4_session", issue_session(role + "@example.test", role,
                                                             SESSION_EXPIRY, SESSION_SECRET))
        for path in ("/v1/system-check", "/v1/support-bundle"):
            response = client.get(path)
            assert response.status_code == 403, (role, path, response.status_code)
            assert response.json()["detail"] == "not available for your role"

    client.cookies.set("aiflow4_session", issue_session("sa@example.test", "sa", SESSION_EXPIRY,
                                                         SESSION_SECRET))
    assert client.get("/v1/system-check").status_code == 200
    assert client.get("/v1/support-bundle").status_code == 200


def test_system_check_and_support_bundle_respect_project_scope(ctx):
    ctx.repo().projects["OTHER"] = Project("OTHER", "jooservices/other")
    node_app = create_app(ctx)
    node_app.state.sync_accounts()
    node_client = TestClient(node_app)
    assert join_node(node_client, "cc", project="TESTWP").status_code == 200
    assert join_node(node_client, "node-other", project="OTHER").status_code == 200
    from aiflow.v4.control.db import J

    readiness = ctx.db.one("SELECT readiness FROM nodes WHERE name='cc'")["readiness"] or {}
    readiness.setdefault("projects", {})["OTHER"] = [{
        "id": "hidden-project-condition", "name": "Other project condition", "result": "needs_action",
        "detail": "Private project result.", "action": "Fix the private project condition.",
    }]
    ctx.db.run("UPDATE nodes SET readiness=%s WHERE name='cc'", (J(readiness),))

    login.seed_user(ctx.db, "scoped-operations@example.test", "team_lead")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["TESTWP"], "scoped-operations@example.test"))
    client = role_client(ctx, "team_lead", "scoped-operations@example.test")

    report = client.get("/v1/system-check")
    assert report.status_code == 200
    assert {node["name"] for node in report.json()["nodes"]} == {"cc", "node1"}
    assert "OTHER" not in report.json()["nodes"][0]["readiness"].get("projects", {})
    assert all(set(node["readiness"].get("projects", {})).issubset({"TESTWP"})
               for node in report.json()["nodes"])

    bundle = client.get("/v1/support-bundle")
    assert bundle.status_code == 200
    payload = bundle.json()
    assert payload["projects"] == ["TESTWP"]
    assert {node["name"] for node in payload["system_check"]["nodes"]} == {"cc", "node1"}
    assert "OTHER" not in payload["system_check"]["nodes"][0]["readiness"].get("projects", {})
    assert all(set(node["readiness"].get("projects", {})).issubset({"TESTWP"})
               for node in payload["system_check"]["nodes"])


def test_board_reader_roles_keep_already_done_reason_and_pull_request_in_state(ctx):
    add_ticket(ctx.db, "TESTWP-2", status="In Verify")
    url = "https://github.com/jooservices/test-aiflow-wp/pull/42"
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary,details,pr_url,started_at,ended_at)"
               " VALUES (%s,'TESTWP-2','TESTWP','dev','claude-01','claude','cc',1,'finished','done',%s,%s,%s,now()-interval '1 minute',now())",
               (str(uuid.uuid4()), "Already done: no new changes were needed", J({"already_done": True, "no_changes": True}), url))

    for role in ("delivery", "product_owner"):
        client = role_client(ctx, role, f"{role}@example.test")
        ticket = next(row for row in client.get("/v1/state").json()["tickets"] if row["key"] == "TESTWP-2")

        assert ticket["already_done"]["pr_url"] == url, role
        assert ticket["last_run"]["already_done"] is True, role


def test_usage_analytics_respects_project_scope_for_projects_tickets_and_accounts(ctx):
    ctx.repo().projects["OTHER"] = Project("OTHER", "jooservices/other")
    for ticket, project, account, node, cost in (
            ("TESTWP-901", "TESTWP", "alpha-account", "cc", .01),
            ("OTHER-901", "OTHER", "outside-account", "node-other", .02)):
        add_ticket(ctx.db, ticket)
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
                    "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
                    " VALUES (%s,%s,%s,'dev',%s,'claude','model',%s,1,'finished','done',now()-interval '1 minute',"
                    "now(),100,20,%s)", (str(uuid.uuid4()), ticket, project, account, node, cost))

    login.seed_user(ctx.db, "usage-limited@example.test", "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["TESTWP"], "usage-limited@example.test"))
    limited = role_client(ctx, "business", "usage-limited@example.test")

    by_project = limited.get("/v1/analytics/usage?group_by=project")
    assert by_project.status_code == 200
    assert {row["key"] for row in by_project.json()["rows"]} == {"TESTWP"}
    by_account_filter = limited.get("/v1/analytics/usage?group_by=project&account=outside-account")
    assert by_account_filter.status_code == 200 and by_account_filter.json()["rows"] == []
    for path in ("/v1/analytics/usage?group_by=project&project=OTHER",
                 "/v1/analytics/usage?group_by=ticket&ticket=OTHER-901"):
        refused = limited.get(path)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "not available for your role"

    # Account and machine dimensions reveal shared infrastructure and need the Machines and AI permission.
    for dimension in ("account", "machine"):
        refused = limited.get(f"/v1/analytics/usage?group_by={dimension}")
        assert refused.status_code == 403
        assert refused.json()["detail"] == "not available for your role"
    denied_paths = {row["payload"]["path"] for row in ctx.db.all(
        "SELECT payload FROM write_audit WHERE action='access_denied' AND actor=%s", ("usage-limited@example.test",))}
    assert "/v1/analytics/usage" in denied_paths

    login.seed_user(ctx.db, "usage-sa@example.test", "sa")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (["TESTWP"], "usage-sa@example.test"))
    sa = role_client(ctx, "sa", "usage-sa@example.test")
    scoped_accounts = sa.get("/v1/analytics/usage?group_by=account")
    assert scoped_accounts.status_code == 200
    assert {row["key"] for row in scoped_accounts.json()["rows"]} == {"alpha-account"}


def test_usage_and_infrastructure_apis_follow_role_permissions_for_every_role(ctx):
    client = role_client(ctx)
    for role in CANONICAL_ROLES:
        subject = f"usage-{role}@example.test"
        client.cookies.set("aiflow4_session", issue_session(subject, role, SESSION_EXPIRY, SESSION_SECRET))

        # Project/ticket aggregates belong to the common Dashboard; account and machine aggregates do not.
        assert client.get("/v1/analytics/usage?group_by=project").status_code == 200, role
        can_view_machines = has_permission(role, "view.machines_ai")
        for dimension in ("account", "machine"):
            response = client.get(f"/v1/analytics/usage?group_by={dimension}")
            assert response.status_code == (200 if can_view_machines else 403), (role, dimension, response.text)
            if not can_view_machines:
                assert response.json()["detail"] == "not available for your role"

        for path in ("/v1/role-health", "/v1/master/autonomy", "/v1/master/report", "/v1/provider-routing",
                     "/v1/provider-routing/decisions"):
            response = client.get(path)
            assert response.status_code == (200 if can_view_machines else 403), (role, path, response.text)
            if not can_view_machines:
                assert response.json()["detail"] == "not available for your role"
                refusal = ctx.db.one("SELECT actor,accepted,result,payload FROM write_audit WHERE target='console'"
                                     " AND action='access_denied' AND actor=%s ORDER BY id DESC LIMIT 1", (subject,))
                assert refusal and not refusal["accepted"] and refusal["result"] == "not available for your role"
                assert refusal["payload"]["path"] == path


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


def test_console_has_screen_paths_for_people_management_and_view_as():
    page = console_template()
    assert page.count("function renderUsers()") == 1
    assert "data-user-add" in page and "data-user-save" in page and "data-user-toggle" in page
    assert "id=\"view-as-role\"" in page and "/v1/console/view-as" in page


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


def test_master_autonomy_and_report_only_cover_the_projects_of_a_limited_person(ctx):
    import uuid

    from test_reviews import add_review_run

    ctx.repo().projects["OTHER"] = Project("OTHER", "jooservices/other")
    for key in ("TESTWP-7", "OTHER-7"):
        add_ticket(ctx.db, key, status="Done", labels=())
        add_review_run(ctx, key, summary="done")
        ctx.db.run("UPDATE tickets SET done_at=now() WHERE key=%s", (key,))
        ctx.db.run("INSERT INTO master_escalations (observation_key, ticket, reason, run_id) VALUES (%s,%s,%s,%s)",
                   (f"obs-{key}", key, f"{key} needs a decision", str(uuid.uuid4())))
        ctx.db.run("INSERT INTO flow_bug_tracker (signature, ticket_key, repeat_count) VALUES (%s,%s,2)",
                   (f"sig-{key}", key))
    login.seed_user(ctx.db, "scoped@example.test", "sa")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (["TESTWP"], "scoped@example.test"))
    login.seed_user(ctx.db, "everything@example.test", "sa")

    limited = role_client(ctx, "sa", "scoped@example.test")
    unrestricted = role_client(ctx, "sa", "everything@example.test")
    for path in ("/v1/master/autonomy", "/v1/master/report"):
        everyone = unrestricted.get(path)
        assert everyone.status_code == 200
        report = everyone.json().get("report", everyone.json())
        assert {row["ticket"] for row in report["needed_person"]} == {"TESTWP-7", "OTHER-7"}
        assert {row["key"] for row in report["finished_without_person"]} == {"TESTWP-7", "OTHER-7"}

        scoped = limited.get(path)
        assert scoped.status_code == 200
        report = scoped.json().get("report", scoped.json())
        assert {row["ticket"] for row in report["needed_person"]} == {"TESTWP-7"}
        assert {row["key"] for row in report["finished_without_person"]} == {"TESTWP-7"}
        assert {row["ticket_key"] for row in report["repeated_failures"]} == {"TESTWP-7"}
        assert "OTHER" not in scoped.text
        assert report["quota_accounts"] == []


def test_review_actions_follow_the_review_actions_permission_of_each_role(ctx):
    from test_reviews import add_review_ticket

    allowed_roles = {role for role in CANONICAL_ROLES if "review.actions" in ROLE_PERMISSIONS[role]
                     or "*" in ROLE_PERMISSIONS[role]}
    assert allowed_roles == {"administrator", "product_owner", "delivery", "team_lead", "reviewer"}
    for number, role in enumerate(CANONICAL_ROLES, start=300):
        key = f"TESTWP-{number}"
        add_review_ticket(ctx, key)
        client = role_client(ctx, role, f"{role}@example.test")
        response = client.post(f"/v1/reviews/{key}/approve")  # no reviewer bearer token
        if role in allowed_roles:
            assert response.status_code == 200 and response.json()["merged"] is True, (role, response.text)
            assert ctx.jira.status_of[key] == "Done"
        else:
            assert response.status_code == 403, (role, response.status_code)
            assert response.json()["detail"] == "not available for your role"
            assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))["status"] == "Human review"
            audit = ctx.db.one("SELECT accepted,payload FROM write_audit WHERE action='access_denied'"
                               " AND actor=%s ORDER BY id DESC LIMIT 1", (f"{role}@example.test",))
            assert audit and not audit["accepted"] and audit["payload"]["path"].endswith("/approve")


def test_review_action_stays_inside_the_project_scope_and_keeps_the_legacy_token(ctx):
    from test_reviews import add_review_ticket

    ctx.repo().projects["OTHER"] = Project("OTHER", "jooservices/other")
    ctx.github.repositories.append({"full_name": "jooservices/other", "name": "other", "default_branch": "develop"})
    add_review_ticket(ctx, "TESTWP-310")
    add_review_ticket(ctx, "OTHER-310")
    login.seed_user(ctx.db, "scoped-reviewer@example.test", "reviewer")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (["TESTWP"], "scoped-reviewer@example.test"))
    client = role_client(ctx, "reviewer", "scoped-reviewer@example.test")

    refused = client.post("/v1/reviews/OTHER-310/rework", json={"comment": "Please change the tests."})
    assert refused.status_code == 403 and refused.json()["detail"] == "not available for your role"
    allowed = client.post("/v1/reviews/TESTWP-310/rework", json={"comment": "Please change the tests."})
    assert allowed.status_code == 200, allowed.text

    # Without a session the reviewer token still works; a wrong or missing token is refused.
    legacy = TestClient(create_app(ctx))
    assert legacy.post("/v1/reviews/OTHER-310/rework", json={"comment": "x"}).status_code == 401
    bad = legacy.post("/v1/reviews/OTHER-310/rework", json={"comment": "x"}, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    good = legacy.post("/v1/reviews/OTHER-310/rework", json={"comment": "Please change the tests."},
                       headers={"Authorization": "Bearer review-secret"})
    assert good.status_code == 200, good.text
