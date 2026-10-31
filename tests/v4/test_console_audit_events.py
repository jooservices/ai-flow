from __future__ import annotations

import ast
from pathlib import Path

from aiflow.v4.control import licensing, login
from test_console_roles import role_client
from test_licensing import FakeResponse, issuer_key, sign_in as sign_in_licensing, token
from test_notifications import add_channel, sign_in as sign_in_notifications


ROOT = Path(__file__).resolve().parents[2]


def assert_event_and_audit(db, event_type, *, aggregate, actor, event_fields, target, action, result, payload):
    events = db.all("SELECT type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s", (event_type,))
    assert events == [{"type": event_type, "aggregate": aggregate, "aggregate_id": actor,
                       "payload": {"actor": actor, **event_fields}}]
    rows = db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                  "WHERE target=%s AND action=%s", (target, action))
    assert rows == [{"target": target, "action": action, "ticket": "", "run_id": "", "fence": None,
                     "actor": actor, "accepted": True, "result": result, "payload": payload}]


def test_console_view_as_publishes_one_event_and_writes_the_same_audit_row(ctx, db):
    actor = "view-admin@example.test"
    login.seed_user(ctx.db, actor, "administrator")
    client = role_client(ctx, "administrator", actor)

    response = client.post("/v1/console/view-as", json={"role": "reviewer"})

    assert response.status_code == 200
    assert_event_and_audit(db, "console.view_as_started", aggregate="console_user", actor=actor,
                           event_fields={"role": "reviewer"}, target="console", action="view_as",
                           result="Administrator viewing as role", payload={"role": "reviewer"})


def test_console_stop_view_as_publishes_one_event_and_writes_the_same_audit_row(ctx, db):
    actor = "view-admin@example.test"
    login.seed_user(ctx.db, actor, "administrator")
    client = role_client(ctx, "administrator", actor)
    assert client.post("/v1/console/view-as", json={"role": "reviewer"}).status_code == 200

    response = client.post("/v1/console/view-as/stop", json={})

    assert response.status_code == 200
    assert_event_and_audit(db, "console.view_as_stopped", aggregate="console_user", actor=actor,
                           event_fields={"previous_role": "reviewer"}, target="console", action="view_as_stop",
                           result="Administrator returned to own role", payload={"previous_role": "reviewer"})


def test_notification_channel_save_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    actor = "admin@example.test"
    sign_in_notifications(client, ctx)

    response = client.put("/v1/notifications/channels", json={
        "name": "Operations email", "kind": "email",
        "config": {"host": "mail.example.test", "from": "flow@example.test", "to": "ops@example.test"},
    })

    assert response.status_code == 200
    channel = response.json()["channel"]
    payload = {"channel_id": channel["id"], "kind": channel["kind"], "enabled": channel["enabled"]}
    assert_event_and_audit(db, "notifications.channel_saved", aggregate="notification", actor=actor,
                           event_fields=payload, target="notifications", action="channel_save",
                           result="notification channel saved", payload=payload)


def test_notification_channel_delete_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    actor = "admin@example.test"
    sign_in_notifications(client, ctx)
    channel = add_channel(client, "email", {"host": "mail.example.test", "from": "flow@example.test",
                                             "to": "ops@example.test"}, "Operations email")

    response = client.delete(f"/v1/notifications/channels/{channel['id']}")

    assert response.status_code == 200
    payload = {"channel_id": channel["id"]}
    assert_event_and_audit(db, "notifications.channel_deleted", aggregate="notification", actor=actor,
                           event_fields=payload, target="notifications", action="channel_delete",
                           result="notification channel deleted", payload=payload)


def test_notification_preferences_save_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    actor = "admin@example.test"
    sign_in_notifications(client, ctx)

    response = client.put("/v1/notifications/preferences", json={
        "routes": {"critical_incident": []}, "quiet_start": "22:00", "quiet_end": "06:00",
    })

    assert response.status_code == 200
    event_fields = {"route_count": 0, "quiet_hours_enabled": True}
    assert_event_and_audit(db, "notifications.preferences_saved", aggregate="notification", actor=actor,
                           event_fields=event_fields, target="notifications", action="preferences_save",
                           result="notification routing preferences saved", payload=event_fields)


def test_activate_license_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db, monkeypatch):
    actor = "owner@example.test"
    private, public = issuer_key()
    sign_in_licensing(client, ctx)
    ctx.settings.license_public_key = public
    ctx.settings.license_service_url = "https://licenses.example.test/activate"
    installation_id = client.get("/v1/licensing").json()["installation_id"]
    signed_license = token(private, installation_id, plan="team")
    monkeypatch.setattr(licensing.httpx, "post", lambda *args, **kwargs: FakeResponse({"license_token": signed_license}))

    response = client.post("/v1/licensing/activate", json={"key": "customer-key"})

    assert response.status_code == 200
    event_fields = {"source": "online", "plan": "team"}
    assert_event_and_audit(db, "licensing.license_activated", aggregate="licence", actor=actor,
                           event_fields=event_fields, target="licensing", action="license_activate",
                           result="activated online", payload=event_fields)


def test_activate_license_offline_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    actor = "owner@example.test"
    private, public = issuer_key()
    sign_in_licensing(client, ctx)
    ctx.settings.license_public_key = public
    installation_id = client.get("/v1/licensing").json()["installation_id"]
    signed_license = token(private, installation_id, plan="team")

    response = client.post("/v1/licensing/offline", json={"license_file": signed_license})

    assert response.status_code == 200
    event_fields = {"source": "offline", "plan": "team"}
    assert_event_and_audit(db, "licensing.license_activated", aggregate="licence", actor=actor,
                           event_fields=event_fields, target="licensing", action="license_activate",
                           result="activated offline", payload=event_fields)


def test_update_license_limits_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    actor = "owner@example.test"
    sign_in_licensing(client, ctx)
    limits = {"machines": 2, "ai_accounts": 4, "projects": 2, "users": 2}

    response = client.put("/v1/licensing/limits", json={"plan": "free", "limits": limits})

    assert response.status_code == 200
    event_fields = {"plan": "free", "limits": limits}
    assert_event_and_audit(db, "licensing.limits_updated", aggregate="licence", actor=actor,
                           event_fields=event_fields, target="licensing", action="license_limits",
                           result="limits updated", payload=event_fields)


def test_the_console_routes_no_longer_make_plain_audit_calls():
    routes = ROOT / "aiflow/v4/control/features/console/routes.py"
    module = ast.parse(routes.read_text(encoding="utf-8"))

    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "audit"
                   for node in ast.walk(module))
