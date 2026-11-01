"""Adding, updating, disabling and enabling a console person publish events whose audit rows match the old ones."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import issue_session

SESSION_SECRET = "test-console-user-audit-secret-with-at-least-32-bytes"
ADMIN = "admin@example.test"
PERSON = "new-person@example.test"


def admin_client(ctx) -> TestClient:
    login.seed_user(ctx.db, ADMIN, "super_admin", "Site Admin")
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SESSION_SECRET
    app = create_app(ctx)
    app.state.sync_accounts()
    client = TestClient(app)
    client.cookies.set("aiflow4_session", issue_session(ADMIN, "super_admin", 2_000_000_000, SESSION_SECRET))
    return client


def events_of(db, event_type):
    return db.all("SELECT type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s ORDER BY seq",
                  (event_type,))


def audit_rows(db, action):
    return db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                  "WHERE target='console' AND action=%s ORDER BY id", (action,))


def expected_row(action, result, payload):
    return {"target": "console", "action": action, "ticket": "", "run_id": "", "fence": None, "actor": ADMIN,
            "accepted": True, "result": result, "payload": payload}


def test_add_update_disable_and_enable_publish_events_with_the_same_audit_rows(ctx):
    client = admin_client(ctx)
    added = client.post("/v1/console/users", json={"email": PERSON, "display_name": "New Person",
                                                  "roles": ["lead"], "project_keys": ["TESTWP"]})
    assert added.status_code == 200, added.text
    assert events_of(ctx.db, "console.user_added") == [
        {"type": "console.user_added", "aggregate": "console_user", "aggregate_id": PERSON,
         "payload": {"actor": ADMIN, "email": PERSON, "roles": ["team_lead"], "project_keys": ["TESTWP"]}}]
    assert audit_rows(ctx.db, "user_add") == [expected_row(
        "user_add", "Person added", {"email": PERSON, "roles": ["team_lead"], "project_keys": ["TESTWP"]})]

    updated = client.put(f"/v1/console/users/{PERSON}", json={"roles": ["ba", "business"], "project_keys": []})
    assert updated.status_code == 200, updated.text
    assert events_of(ctx.db, "console.user_updated") == [
        {"type": "console.user_updated", "aggregate": "console_user", "aggregate_id": PERSON,
         "payload": {"actor": ADMIN, "email": PERSON, "roles": ["ba", "business"], "project_keys": []}}]
    assert audit_rows(ctx.db, "user_update") == [expected_row(
        "user_update", "Person updated", {"email": PERSON, "roles": ["ba", "business"], "project_keys": []})]

    assert client.patch(f"/v1/console/users/{PERSON}/disabled", json={"disabled": True}).status_code == 200
    assert client.patch(f"/v1/console/users/{PERSON}/disabled", json={"disabled": False}).status_code == 200
    assert [e["payload"]["disabled"] for e in events_of(ctx.db, "console.user_disabled_changed")] == [True, False]
    assert audit_rows(ctx.db, "user_disable") == [expected_row("user_disable", "Person disabled", {"email": PERSON})]
    assert audit_rows(ctx.db, "user_enable") == [expected_row("user_enable", "Person enabled", {"email": PERSON})]


def test_a_failed_event_rolls_the_new_person_back(ctx, monkeypatch):
    client = admin_client(ctx)

    def fail_publish(*args, **kwargs):
        raise RuntimeError("forced event failure")

    monkeypatch.setattr("aiflow.v4.control.features.console.routes.publish_for", fail_publish)
    refused = client.post("/v1/console/users", json={"email": PERSON, "display_name": "New Person",
                                                    "roles": ["lead"], "project_keys": []})
    assert refused.status_code == 500
    assert ctx.db.one("SELECT count(*) AS n FROM console_users WHERE email=%s", (PERSON,))["n"] == 0
    assert events_of(ctx.db, "console.user_added") == []
    assert audit_rows(ctx.db, "user_add") == []
