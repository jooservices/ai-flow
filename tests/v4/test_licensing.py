"""Licence activation, resource limits, usage reports and grace/read-only behavior."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiflow.v4.control import incidents, licensing, login
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import J
from aiflow.v4.control.ui import mount_ui
from conftest import add_ticket, join_node
from aiflow.v4.control.ui import console_template

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "v4" / "control" / "compose.yaml"
LICENSE_PUBLIC_KEY = Ed25519PrivateKey.generate().public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
SESSION_SECRET = "licensing-console-session-secret-has-32-bytes"


def expire_grace(ctx):
    """Pretend usage above the limits was first detected longer than the grace period ago."""
    ctx.db.run("UPDATE licensing_state SET over_limit_since=now() - interval '15 days' WHERE id=1")


def sign_in(client, ctx, role="owner"):
    licensing.initialize(ctx.db)
    ctx.settings.license_public_key = LICENSE_PUBLIC_KEY
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(licensing.PLAN_DEFAULTS),))
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("owner@example.test", role, 2_000_000_000,
                                                         SESSION_SECRET))


def issuer_key():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


def token(private, installation_id, *, plan="team", expiry=None):
    claims = {"plan": plan, "exp": int((expiry or datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),
              "installation_id": installation_id}
    return jwt.encode(claims, private, algorithm="EdDSA")


def signed_in_license(client, ctx, private, public, *, plan="team", expiry=None):
    sign_in(client, ctx)
    ctx.settings.license_public_key = public
    installation_id = client.get("/v1/licensing").json()["installation_id"]
    license_token = token(private, installation_id, plan=plan, expiry=expiry)
    return installation_id, license_token


class FakeResponse:
    def __init__(self, body=None, error=None):
        self.body, self.error = body or {}, error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.body


def test_plan_defaults_and_console_licence_usage_export_path(client, ctx):
    sign_in(client, ctx)
    response = client.get("/v1/licensing")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active" and data["plan"] == "free"
    assert data["plans"] == {
        "free": {"machines": 3, "ai_accounts": 6, "projects": 3, "users": 3},
        "team": {"machines": 5, "ai_accounts": 12, "projects": 25, "users": 25},
        "business": {"machines": 50, "ai_accounts": 100, "projects": 250, "users": 250},
    }
    page = console_template()
    assert 'data-view="licensing">Licence</button>' in page
    assert 'id="license-warning"' in page and "14-day grace period" in page
    assert "state.sync_warnings" in page
    for path in ("/v1/licensing/status", "/v1/licensing/activate", "/v1/licensing/offline", "/v1/licensing/limits",
                 "/v1/licensing/usage.csv", "Export spreadsheet"):
        assert path in page
    app = FastAPI()
    mount_ui(app, ctx)
    assert TestClient(app).get("/licensing").status_code == 200
    assert 'PathPrefix(`/`) && !PathPrefix(`/o2`) && !PathPrefix(`/gatus`)' in COMPOSE.read_text()


def test_database_migration_starts_with_the_published_plan_defaults(db):
    licensing.initialize(db)
    row = db.one("SELECT plan_limits FROM licensing_state WHERE id=1")
    assert row["plan_limits"] == licensing.PLAN_DEFAULTS


def test_console_readers_get_the_installation_warning_status_without_licence_management(client, ctx):
    sign_in(client, ctx, role="lead")
    response = client.get("/v1/licensing/status")
    assert response.status_code == 200
    assert response.json() == {"status": "active", "reason": "", "plan": "free", "expires_at": None,
                               "grace_until": None, "read_only": False, "licence_required": True}
    assert client.get("/v1/licensing").status_code == 403


def test_online_activation_sends_only_key_and_installation_identifier(client, ctx, monkeypatch):
    private, public = issuer_key()
    sign_in(client, ctx)
    ctx.settings.license_public_key = public
    ctx.settings.license_service_url = "https://licenses.example.test/activate"
    installation_id = client.get("/v1/licensing").json()["installation_id"]
    signed = token(private, installation_id, plan="team")
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return FakeResponse({"license_token": signed})

    monkeypatch.setattr(licensing.httpx, "post", post)
    response = client.post("/v1/licensing/activate", json={"key": "secret-customer-key"})
    assert response.status_code == 200, response.text
    assert response.json()["plan"] == "team" and response.json()["status"] == "active"
    assert len(requests) == 1
    url, request = requests[0]
    assert url == ctx.settings.license_service_url and request["follow_redirects"] is False
    assert request["json"] == {"key": "secret-customer-key", "installation_id": installation_id}
    assert set(request["json"]) == {"key", "installation_id"}
    assert not any(word in str(request["json"]).lower() for word in ("ticket", "code", "credential", "password"))
    stored = ctx.db.one("SELECT license_token,license_source FROM licensing_state WHERE id=1")
    assert stored == {"license_token": signed, "license_source": "online"}


def test_successful_license_configuration_changes_are_write_audited_without_operator_bugs(client, ctx, monkeypatch):
    from aiflow.v4.control import flow_doctor

    private, public = issuer_key()
    sign_in(client, ctx)
    ctx.settings.license_public_key = public
    ctx.settings.license_service_url = "https://licenses.example.test/activate"
    installation_id = client.get("/v1/licensing").json()["installation_id"]
    signed = token(private, installation_id, plan="team")
    monkeypatch.setattr(licensing.httpx, "post", lambda *a, **k: FakeResponse({"license_token": signed}))

    online = client.post("/v1/licensing/activate", json={"key": "customer-key"})
    offline = client.post("/v1/licensing/offline", json={"license_file": signed})
    limits = client.put("/v1/licensing/limits", json={
        "plan": "team", "limits": {"machines": 4, "ai_accounts": 10, "projects": 20, "users": 20},
    })
    assert online.status_code == offline.status_code == limits.status_code == 200

    writes = ctx.db.all("SELECT actor,action,accepted FROM write_audit WHERE target='licensing' ORDER BY id")
    assert writes == [
        {"actor": "owner@example.test", "action": "license_activate", "accepted": True},
        {"actor": "owner@example.test", "action": "license_activate", "accepted": True},
        {"actor": "owner@example.test", "action": "license_limits", "accepted": True},
    ]
    assert ctx.db.one("SELECT count(*) AS n FROM operator_interventions")["n"] == 0
    assert flow_doctor.create_intervention_bugs(ctx) == 0
    assert not getattr(ctx.jira, "created", [])


def test_invalid_online_key_and_bad_offline_signature_leave_current_license_unchanged(client, ctx, monkeypatch):
    private, public = issuer_key()
    other_private, _ = issuer_key()
    installation_id, current = signed_in_license(client, ctx, private, public)
    valid = client.post("/v1/licensing/offline", json={"license_file": current})
    assert valid.status_code == 200
    before = ctx.db.one("SELECT plan,expires_at,license_token,license_source,invalid_since,plan_limits"
                        " FROM licensing_state WHERE id=1")

    ctx.settings.license_service_url = "https://licenses.example.test/activate"
    monkeypatch.setattr(licensing.httpx, "post", lambda *a, **k: FakeResponse(error=httpx.HTTPError("rejected")))
    rejected = client.post("/v1/licensing/activate", json={"key": "never-echo-this-key"})
    assert rejected.status_code == 422 and "licence service" in rejected.json()["detail"].lower()
    bad_file = token(other_private, installation_id, plan="business")
    bad = client.post("/v1/licensing/offline", json={"license_file": bad_file})
    assert bad.status_code == 422 and "signature" in bad.json()["detail"].lower()
    after = ctx.db.one("SELECT plan,expires_at,license_token,license_source,invalid_since,plan_limits"
                       " FROM licensing_state WHERE id=1")
    assert after == before
    assert "never-echo-this-key" not in rejected.text and "never-echo-this-key" not in bad.text


def test_oversized_activation_values_are_rejected_without_echoing_input(client, ctx):
    sign_in(client, ctx)
    key = "sensitive-oversized-key-" + ("K" * 2000)
    license_file = "sensitive-oversized-signed-file-" + ("T" * 50000)

    rejected_key = client.post("/v1/licensing/activate", json={"key": key})
    rejected_file = client.post("/v1/licensing/offline", json={"license_file": license_file})

    assert rejected_key.status_code == 422
    assert rejected_file.status_code == 422
    assert "supported size" in rejected_key.json()["detail"]
    assert "supported size" in rejected_file.json()["detail"]
    assert key not in rejected_key.text and "sensitive-oversized-key" not in rejected_key.text
    assert license_file not in rejected_file.text and "sensitive-oversized-signed-file" not in rejected_file.text


def test_limits_are_editable_and_project_machine_account_and_user_caps_are_enforced(client, ctx):
    sign_in(client, ctx)
    updated = client.put("/v1/licensing/limits", json={
        "plan": "free", "limits": {"machines": 1, "ai_accounts": 2, "projects": 1, "users": 1},
    })
    assert updated.status_code == 200 and updated.json()["limits"]["projects"] == 1

    project = client.post("/v1/projects", json={
        "site": "https://jooservices.atlassian.net", "project": "NEWAPP", "repo": "jooservices/new-app",
        "base": "develop", "stack": "", "checks": [], "priority": 5, "parallel_limit": 1,
        "merge_method": "squash",
    })
    assert project.status_code == 409 and "Projects limit of 1" in project.json()["detail"]

    ctx.settings.join_token = "join-secret"
    machine = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": "node-new",
                                                         "role": "worker", "project": "TESTWP"})
    assert machine.status_code == 409 and "Machines limit of 1" in machine.json()["detail"]

    join = join_node(client, "cc")
    assert join.status_code == 200
    expire_grace(ctx)
    account = client.get("/v1/work/candidates", params={"account": "opencode-01"},
                         headers={"Authorization": f"Bearer {join.json()['node_token']}"})
    assert account.status_code == 200 and "AI accounts limit of 2" in account.json()["reason"]

    first = login.seed_user(ctx.db, "first@example.test", "lead", public_key=LICENSE_PUBLIC_KEY)
    assert first
    with pytest.raises(ValueError, match="Users limit of 1"):
        login.seed_user(ctx.db, "second@example.test", "lead", public_key=LICENSE_PUBLIC_KEY)


def test_managed_ai_account_creation_is_refused_at_the_plan_cap(client, ctx):
    sign_in(client, ctx)
    current = licensing.resource_counts(ctx)["ai_accounts"]
    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"]["ai_accounts"] = current
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))
    before = ctx.db.one("SELECT count(*) AS count FROM managed_ai_accounts WHERE removed_at IS NULL")["count"]

    response = client.post("/v1/machines/node1/accounts", json={
        "provider": "grok", "sessions": 1, "jobs": ["develop"],
    })

    assert response.status_code == 409
    assert "AI accounts limit of" in response.json()["detail"]
    assert "raise the limit in the Licence screen" in response.json()["detail"]
    assert ctx.db.one("SELECT count(*) AS count FROM managed_ai_accounts WHERE removed_at IS NULL")["count"] == before
    assert ctx.db.one("SELECT count(*) AS count FROM accounts")["count"] == current


def test_resource_counts_uses_one_database_aggregate_for_entitlement_totals(ctx):
    queries = []
    original_all, original_one = ctx.db.all, ctx.db.one

    def recording_all(sql, params=None):
        queries.append(sql)
        return original_all(sql, params)

    def recording_one(sql, params=None):
        queries.append(sql)
        return original_one(sql, params)

    ctx.db.all = recording_all
    ctx.db.one = recording_one
    counts = licensing.resource_counts(ctx)

    [aggregate] = [sql for sql in queries if " AS ai_accounts," in sql]
    assert "UNION SELECT unnest(%s::text[])" in aggregate
    assert counts["ai_accounts"] == len({account.id for account in ctx.repo().accounts})
    assert counts["machines"] == 0 and counts["users"] == 0


def test_worker_polls_share_a_fresh_licence_resource_count_snapshot(client, ctx):
    sign_in(client, ctx)
    joined = join_node(client, "cc")
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    aggregate_queries = []
    original_one = ctx.db.one

    def recording_one(sql, params=None):
        if " AS ai_accounts," in sql:
            aggregate_queries.append(sql)
        return original_one(sql, params)

    ctx.db.one = recording_one
    for _ in range(2):
        response = client.get("/v1/work/candidates?account=claude-01", headers=headers)
        assert response.status_code == 200

    assert len(aggregate_queries) == 1


def test_startup_sync_skips_configured_accounts_and_machines_over_their_limits(client, ctx):
    sign_in(client, ctx)
    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"]["ai_accounts"] = 2
    limits["free"]["machines"] = 1
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))
    ctx.db.run("DELETE FROM accounts")
    ctx.db.run("DELETE FROM nodes")

    warnings = client.app.state.sync_accounts()

    assert ctx.db.one("SELECT count(*) AS count FROM accounts")["count"] == 2
    assert ctx.db.one("SELECT id FROM accounts WHERE id='opencode-01'") is None
    assert ctx.db.one("SELECT count(*) AS count FROM nodes")["count"] == 1
    assert ctx.db.one("SELECT name FROM nodes WHERE name='node1'") is None
    assert any("AI accounts limit of 2 reached" in message and "Licence screen" in message
               for message in warnings)
    assert any("Machines limit of 1 reached" in message and "Licence screen" in message
               for message in warnings)
    assert client.get("/v1/licensing").json()["sync_warnings"] == warnings
    # The Licence screen reports all configured IDs even when startup skipped one at capacity.
    assert client.get("/v1/licensing").json()["usage"]["ai_accounts"] == 3

    # A configured account omitted from the registry still cannot claim new work by name.
    expire_grace(ctx)
    with pytest.raises(licensing.LicenseError, match="AI accounts limit of 2 reached"):
        licensing.enforce_account_entitlement(ctx, "opencode-01", public_key=ctx.settings.license_public_key)
    ctx.settings.join_token = "join-secret"
    refused = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": "node1",
                                                         "role": "worker", "project": "TESTWP"})
    assert refused.status_code == 409 and "Machines limit of 1" in refused.json()["detail"]


def test_running_work_survives_account_limit_refusal(client, ctx):
    sign_in(client, ctx)
    joined = join_node(client, "cc")
    assert joined.status_code == 200
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome)"
               " VALUES (%s,'TESTWP-90','TESTWP','dev','opencode-01','opencode','cc',1,'running','')", (run_id,))
    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"]["ai_accounts"] = 2
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))
    expire_grace(ctx)
    response = client.get("/v1/work/candidates", params={"account": "opencode-01"},
                          headers={"Authorization": f"Bearer {joined.json()['node_token']}"})
    assert "AI accounts limit" in response.json()["reason"]
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (run_id,))["status"] == "running"


def licensed_over_limit(client, ctx, **limits):
    """A licensed installation (issuer key set) on the Free plan with the given limits."""
    sign_in(client, ctx)
    plans = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    plans["free"].update(limits)
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s, over_limit_since=NULL WHERE id=1", (J(plans),))


def incident(ctx, resource):
    return ctx.db.one("SELECT * FROM incidents WHERE kind='licence_limit' AND subject=%s ORDER BY opened_at DESC",
                      (resource,))


def test_no_licence_configured_means_no_limits_and_no_incident(client, ctx):
    sign_in(client, ctx, role="lead")
    ctx.settings.license_public_key = ""
    ctx.settings.license_service_url = ""
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1",
               (J({plan: {key: 1 for key in v} for plan, v in licensing.PLAN_DEFAULTS.items()}),))
    node = join_node(client, "cc").json()["node_token"]
    join_node(client, "node1")
    state = licensing.snapshot(ctx.db, "")
    assert state["licence_required"] is False and state["limits_enforced"] is False and state["status"] == "active"
    assert client.get("/v1/licensing/status").json()["licence_required"] is False
    assert "No licence required" in console_template()
    offer = client.get("/v1/work/candidates", params={"account": "opencode-01"},
                       headers={"Authorization": f"Bearer {node}"})
    assert "reason" not in offer.json()
    licensing.require_capacity(ctx, "machines", public_key="")
    licensing.require_count_capacity(ctx.db, "users", 99, public_key="")
    assert ctx.db.one("SELECT over_limit_since FROM licensing_state WHERE id=1")["over_limit_since"] is None
    assert incident(ctx, "machines") is None


def test_free_plan_over_limit_keeps_working_for_the_grace_period_then_raises_an_incident(client, ctx):
    licensed_over_limit(client, ctx, machines=1)
    cc = join_node(client, "cc").json()["node_token"]
    node1 = join_node(client, "node1").json()["node_token"]
    add_ticket(ctx.db, "TESTWP-301")

    # First detection starts the grace period; the excess machine still gets work and nothing is raised.
    offer = client.get("/v1/work/candidates", params={"account": "codex-01"},
                       headers={"Authorization": f"Bearer {node1}"})
    assert "reason" not in offer.json()
    first = ctx.db.one("SELECT over_limit_since FROM licensing_state WHERE id=1")["over_limit_since"]
    assert first is not None
    again = client.get("/v1/work/candidates", params={"account": "codex-01"},
                       headers={"Authorization": f"Bearer {node1}"})
    assert "reason" not in again.json()
    assert ctx.db.one("SELECT over_limit_since FROM licensing_state WHERE id=1")["over_limit_since"] == first
    assert incident(ctx, "machines") is None
    # Creating a new machine is refused during the grace period (AC4).
    ctx.settings.join_token = "join-secret"
    refused = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": "node-new",
                                                         "role": "worker", "project": "TESTWP"})
    assert refused.status_code == 409 and "Machines limit of 1" in refused.json()["detail"]

    # The 14-day grace period runs out while usage is still over: the limit stops new work and raises the alarm.
    expire_grace(ctx)
    stopped = client.get("/v1/work/candidates", params={"account": "codex-01"},
                         headers={"Authorization": f"Bearer {node1}"})
    assert "Machines limit of 1 reached" in stopped.json()["reason"]
    opened = incident(ctx, "machines")
    assert opened["severity"] == "critical" and opened["closed_at"] is None
    assert "machines" in opened["title"] and "2/1" in opened["title"]
    assert "Licence screen" in opened["detail"]
    assert client.get("/v1/work/candidates", params={"account": "claude-01"},
                      headers={"Authorization": f"Bearer {cc}"}).status_code == 200

    # The idle-flow alert names the licence limit as the cause (AC3).
    reasons = " ".join(incidents.idle_reasons(ctx, {"TESTWP"}, {"TESTWP-301"}))
    assert "licence limit stops new work" in reasons and "Machines limit of 1" in reasons

    # Raising the limit lets work resume and closes the incident.
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1",
               (J({plan: dict(v) for plan, v in licensing.PLAN_DEFAULTS.items()}),))
    resumed = client.get("/v1/work/candidates", params={"account": "codex-01"},
                         headers={"Authorization": f"Bearer {node1}"})
    assert "reason" not in resumed.json()
    assert incident(ctx, "machines")["closed_at"] is not None
    assert ctx.db.one("SELECT over_limit_since FROM licensing_state WHERE id=1")["over_limit_since"] is None


def test_expired_and_first_detected_invalid_licences_use_their_correct_grace_clock_and_read_only_preserves_data(
        client, ctx):
    private, public = issuer_key()
    installation_id, _ = signed_in_license(client, ctx, private, public)
    expiry = datetime.fromtimestamp(int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp()), timezone.utc)
    expired_token = token(private, installation_id, plan="team", expiry=expiry)
    ctx.db.run("UPDATE licensing_state SET plan='team',expires_at=%s,license_token=%s,license_source='offline' WHERE id=1",
               (expiry, expired_token))
    state = licensing.snapshot(ctx.db, public, expiry + timedelta(days=13, hours=23))
    assert state["status"] == "grace" and state["reason"] == "expired"
    state = licensing.snapshot(ctx.db, public, expiry + timedelta(days=14))
    assert state["status"] == "read_only" and state["read_only"] is True

    # A licence that becomes unverifiable starts its clock on first detection, not at its old expiry.
    bad_token = token(Ed25519PrivateKey.generate(), installation_id, plan="team", expiry=expiry)
    ctx.db.run("UPDATE licensing_state SET license_token=%s,invalid_since=NULL WHERE id=1", (bad_token,))
    detected = datetime.now(timezone.utc)
    assert licensing.snapshot(ctx.db, public, detected)["status"] == "grace"
    assert licensing.snapshot(ctx.db, public, detected + timedelta(days=13))["status"] == "grace"
    assert licensing.snapshot(ctx.db, public, detected + timedelta(days=14))["status"] == "read_only"

    # The actual API refuses new changes after the real grace deadline, leaves data intact, and still accepts renewal.
    expired_at = datetime.fromtimestamp(int((datetime.now(timezone.utc) - timedelta(days=15)).timestamp()), timezone.utc)
    renewal = token(private, installation_id, plan="business", expiry=datetime.now(timezone.utc) + timedelta(days=90))
    ctx.db.run("UPDATE licensing_state SET plan='team',expires_at=%s,license_token=%s,invalid_since=NULL WHERE id=1",
               (expired_at, token(private, installation_id, plan="team", expiry=expired_at)))
    add_ticket(ctx.db, "TESTWP-91")
    before_tickets = ctx.db.one("SELECT count(*) AS count FROM tickets")["count"]
    readonly = client.post("/v1/projects", json={
        "site": "https://jooservices.atlassian.net", "project": "NEWAPP", "repo": "jooservices/new-app",
        "base": "develop", "stack": "", "checks": [], "priority": 5, "parallel_limit": 1,
        "merge_method": "squash",
    })
    assert readonly.status_code == 423 and "read-only" in readonly.json()["detail"]
    limits_before = ctx.db.one("SELECT plan_limits FROM licensing_state WHERE id=1")["plan_limits"]
    limits_refused = client.put("/v1/licensing/limits", json={
        "plan": "free", "limits": {"machines": 10, "ai_accounts": 10, "projects": 10, "users": 10},
    })
    assert limits_refused.status_code == 423 and "read-only" in limits_refused.json()["detail"]
    assert ctx.db.one("SELECT plan_limits FROM licensing_state WHERE id=1")["plan_limits"] == limits_before
    assert ctx.db.one("SELECT count(*) AS count FROM tickets")["count"] == before_tickets
    restored = client.post("/v1/licensing/offline", json={"license_file": renewal})
    assert restored.status_code == 200 and restored.json()["status"] == "active"
    assert ctx.db.one("SELECT count(*) AS count FROM tickets")["count"] == before_tickets


def test_missing_verification_key_records_invalid_grace_without_refusing_writes(client, ctx):
    private, public = issuer_key()
    installation_id, license_token = signed_in_license(client, ctx, private, public)
    activated = client.post("/v1/licensing/offline", json={"license_file": license_token})
    assert activated.status_code == 200

    ctx.settings.license_public_key = ""
    response = client.get("/v1/licensing")
    assert response.status_code == 200
    state = response.json()
    assert state["status"] == "grace" and state["reason"] == "invalid" and not state["read_only"]
    assert state["installation_id"] == installation_id
    saved = ctx.db.one("SELECT invalid_since FROM licensing_state WHERE id=1")
    assert saved["invalid_since"] is not None
    assert licensing.require_writable(ctx.db, "")["status"] == "grace"


def test_malformed_verification_key_records_invalid_grace_and_rejects_activation_clearly(client, ctx):
    private, public = issuer_key()
    installation_id, license_token = signed_in_license(client, ctx, private, public)
    activated = client.post("/v1/licensing/offline", json={"license_file": license_token})
    assert activated.status_code == 200
    ctx.settings.license_public_key = "not a valid EdDSA public key"

    status = client.get("/v1/licensing")
    assert status.status_code == 200
    assert status.json()["status"] == "grace" and status.json()["reason"] == "invalid"
    assert not status.json()["read_only"]
    before = ctx.db.one("SELECT plan,expires_at,license_token,license_source,invalid_since,plan_limits"
                        " FROM licensing_state WHERE id=1")
    assert before["invalid_since"] is not None

    renewed = token(private, installation_id, plan="business")
    rejected = client.post("/v1/licensing/offline", json={"license_file": renewed})
    assert rejected.status_code == 422
    assert "misconfigured" in rejected.json()["detail"].lower()
    assert "AIFLOW4_LICENSE_PUBLIC_KEY" in rejected.json()["detail"]
    assert ctx.db.one("SELECT plan,expires_at,license_token,license_source,invalid_since,plan_limits"
                      " FROM licensing_state WHERE id=1") == before


def test_excess_machines_and_projects_cannot_start_work_but_existing_runs_can_finish(client, ctx):
    sign_in(client, ctx)
    cc = join_node(client, "cc").json()["node_token"]
    node1 = join_node(client, "node1").json()["node_token"]
    repo = ctx.repo()
    from aiflow.v4.core.controlrepo import Project
    repo.projects["ZZZ"] = Project(key="ZZZ", repo="jooservices/another-project")
    repo.nodes["cc"].projects.append("ZZZ")
    ctx.db.run("UPDATE nodes SET projects=%s,status='online' WHERE name='cc'", (["TESTWP", "ZZZ"],))
    ctx.github.repositories.append({"full_name": "jooservices/another-project", "name": "another-project",
                                    "default_branch": "develop"})
    from aiflow.v4.control.system_check import refresh_node
    refresh_node(ctx, "cc", "project_added")
    add_ticket(ctx.db, "TESTWP-201")
    add_ticket(ctx.db, "ZZZ-1")

    machine_run = str(uuid.uuid4())
    machine_lease = ctx.leases.claim("TESTWP-200", machine_run, ctx.settings.lease_ms)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome)"
               " VALUES (%s,'TESTWP-200','TESTWP','dev','codex-01','codex','node1',%s,'running','')",
               (machine_run, machine_lease.fence))
    project_run = str(uuid.uuid4())
    project_lease = ctx.leases.claim("ZZZ-2", project_run, ctx.settings.lease_ms)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome)"
               " VALUES (%s,'ZZZ-2','ZZZ','dev','claude-01','claude','cc',%s,'running','')",
               (project_run, project_lease.fence))

    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"].update(machines=1, projects=1)
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))
    expire_grace(ctx)

    machine_offer = client.get("/v1/work/candidates", params={"account": "codex-01"},
                               headers={"Authorization": f"Bearer {node1}"})
    assert machine_offer.json()["tickets"] == []
    assert "Machines limit of 1" in machine_offer.json()["reason"]
    machine_claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-201", "account": "codex-01"},
                                headers={"Authorization": f"Bearer {node1}"})
    assert machine_claim.status_code == 409 and "Machines limit of 1" in machine_claim.json()["reason"]

    ctx.db.run("DELETE FROM tickets WHERE key='TESTWP-201'")
    project_offer = client.get("/v1/work/candidates", params={"account": "claude-01"},
                               headers={"Authorization": f"Bearer {cc}"})
    assert project_offer.json()["tickets"] == []
    assert "Projects limit of 1" in project_offer.json()["reason"]
    project_claim = client.post("/v1/work/claim", json={"ticket": "ZZZ-1", "account": "claude-01"},
                                headers={"Authorization": f"Bearer {cc}"})
    assert project_claim.status_code == 409 and "Projects limit of 1" in project_claim.json()["reason"]

    for run_id, node_token, fence in ((machine_run, node1, machine_lease.fence),
                                      (project_run, cc, project_lease.fence)):
        heartbeat = client.post(f"/v1/runs/{run_id}/heartbeat", json={"fence": fence, "progress": {"phase": "work"}},
                                headers={"Authorization": f"Bearer {node_token}"})
        assert heartbeat.status_code == 200
        assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (run_id,))["status"] == "running"


def test_concurrent_machine_admissions_cannot_take_the_same_last_slot(client, ctx):
    sign_in(client, ctx)
    ctx.db.run("DELETE FROM node_join_tokens")
    ctx.db.run("DELETE FROM nodes")
    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"]["machines"] = 1
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))

    def request(name):
        return client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": name,
                                                           "role": "worker", "project": "TESTWP"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, ("node-a", "node-b")))
    assert sorted(response.status_code for response in responses) == [200, 409]
    refused = next(response for response in responses if response.status_code == 409)
    assert "Machines limit of 1" in refused.json()["detail"]
    assert ctx.db.one("SELECT count(*) AS count FROM nodes")["count"] == 1


def test_usage_totals_per_project_month_and_csv_export(client, ctx):
    sign_in(client, ctx)
    jan = datetime(2026, 1, 10, tzinfo=timezone.utc)
    feb = datetime(2026, 2, 10, tzinfo=timezone.utc)
    values = [
        ("TESTWP-1", "TESTWP", "done", jan, 100, 20, 0.50),
        ("TESTWP-1", "TESTWP", "done", jan + timedelta(hours=1), 200, 30, 0.25),
        ("TESTWP-2", "TESTWP", "failed", jan, 300, 40, 0.10),
        ("OTHER-1", "OTHER", "done", feb, 400, 50, 0.75),
    ]
    for ticket, project, outcome, started, tokens_in, tokens_out, cost in values:
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                   "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
                   " VALUES (%s,%s,%s,'dev','claude-01','claude','cc',1,'finished',%s,%s,%s,%s,%s,%s)",
                   (str(uuid.uuid4()), ticket, project, outcome, started, started + timedelta(minutes=5),
                    tokens_in, tokens_out, cost))
    ctx.db.run("INSERT INTO tickets (key,project,status,done_at) VALUES"
               " ('TESTWP-1','TESTWP','In Progress',%s),('TESTWP-2','TESTWP','Done',%s),"
               " ('OTHER-1','OTHER','To Do',NULL),('OTHER-2','OTHER','Done',%s)",
               (jan + timedelta(days=5), feb + timedelta(days=2), datetime(2026, 3, 3, tzinfo=timezone.utc)))
    report = client.get("/v1/licensing/usage")
    assert report.status_code == 200
    rows = {(row["project"], row["month"]): row for row in report.json()["rows"]}
    assert rows[("TESTWP", "2026-01")] == {"project": "TESTWP", "month": "2026-01", "tickets_finished": 0,
                                              "tokens_in": 600, "tokens_out": 90, "cost_usd": 0.85}
    assert rows[("TESTWP", "2026-02")] == {"project": "TESTWP", "month": "2026-02", "tickets_finished": 1,
                                              "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    assert rows[("OTHER", "2026-02")]["tickets_finished"] == 0
    assert rows[("OTHER", "2026-03")] == {"project": "OTHER", "month": "2026-03", "tickets_finished": 1,
                                             "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    exported = client.get("/v1/licensing/usage.csv")
    assert exported.status_code == 200 and "text/csv" in exported.headers["content-type"]
    assert "attachment; filename=ai-flow-usage.csv" in exported.headers["content-disposition"]
    assert "TESTWP,2026-01,0,600,90,0.850000" in exported.text
