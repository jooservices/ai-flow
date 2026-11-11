"""JAIF-349: the Dashboard's review request reaches one snapshot, and the cycle metrics count AI calls and actions."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control import licensing, login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import J
from aiflow.v4.control.features.runs.master_cycles import no_action_percent
from conftest import join_node

SESSION_SECRET = "test-master-review-request-secret-with-at-least-32-bytes"
ADMIN = "owner@example.test"
MASTER_SCOPE = "master:global"


def console_app(ctx):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SESSION_SECRET
    app = create_app(ctx)
    app.state.sync_accounts()
    return app


def owner_console(ctx, app) -> TestClient:
    login.seed_user(ctx.db, ADMIN, "super_admin", "Owner")
    app.state.sync_accounts()
    client = TestClient(app)
    client.cookies.set("aiflow4_session", issue_session(ADMIN, "super_admin", 2_000_000_000, SESSION_SECRET))
    return client


def role_console(ctx, app, email: str, role: str) -> TestClient:
    login.seed_user(ctx.db, email, role, role.title())
    app.state.sync_accounts()
    client = TestClient(app)
    client.cookies.set("aiflow4_session", issue_session(email, role, 2_000_000_000, SESSION_SECRET))
    return client


def ready(client, node, account):
    joined = join_node(client, node)
    assert joined.status_code == 200
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    response = client.post(f"/v1/nodes/{node}/heartbeat", json={
        "workers": [{"account": account, "state": "idle"}], "slots_total": 2,
        "metrics": {"hostname": node, "load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 20,
                    "disk_total": 100 * 1024 ** 3, "disk_free": 20 * 1024 ** 3, "disk_free_pct": 20},
    }, headers=headers)
    assert response.status_code == 200
    return headers


def claim_master(client, headers):
    response = client.post("/v1/work/claim", json={"ticket": MASTER_SCOPE, "account": "claude-01", "step": "master"},
                           headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def context(client, headers, master):
    response = client.get("/v1/master/context", params={"run_id": master["run_id"], "fence": master["fence"]},
                          headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def insert_master_run(db, started_interval: str, details: dict) -> None:
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,details,"
           " started_at) VALUES (%s,%s,'',%s,'claude-01','claude','opus','cc',1,'finished','done',%s,"
           " now() - %s::interval)",
           (str(uuid.uuid4()), MASTER_SCOPE, "master", J(details), started_interval))


def test_a_dashboard_request_reaches_exactly_one_snapshot(ctx):
    app = console_app(ctx)
    client = TestClient(app)
    cc = ready(client, "cc", "claude-01")
    ready(client, "node1", "codex-01")
    master = claim_master(client, cc)
    assert context(client, cc, master)["review_requested"] is False

    owner = owner_console(ctx, app)
    response = owner.post("/v1/master/review-requests")
    assert response.status_code == 200 and response.json() == {"requested": True}

    assert context(client, cc, master)["review_requested"] is True
    assert context(client, cc, master)["review_requested"] is False


def test_only_a_console_writer_can_request_a_review(ctx):
    app = console_app(ctx)
    client = TestClient(app)
    audits_before = ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"]
    assert client.post("/v1/master/review-requests").status_code == 401
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"] == audits_before
    assert ctx.db.one("SELECT review_requested_at FROM master_settings WHERE singleton=true")[
        "review_requested_at"] is None


def test_a_console_role_without_write_permission_is_refused_and_audited(ctx):
    app = console_app(ctx)
    stakeholder = role_console(ctx, app, "stakeholder@example.test", "stakeholder")
    audits_before = ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"]

    response = stakeholder.post("/v1/master/review-requests")

    assert response.status_code == 403 and response.json() == {"detail": "not available for your role"}
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"] == audits_before + 1
    assert ctx.db.one("SELECT accepted,result,target,action FROM write_audit ORDER BY id DESC LIMIT 1") == {
        "accepted": False, "result": "not available for your role", "target": "master", "action": "review-request"}
    assert ctx.db.one("SELECT review_requested_at FROM master_settings WHERE singleton=true")[
        "review_requested_at"] is None


def test_a_non_writable_license_prevents_a_review_request(ctx, monkeypatch):
    app = console_app(ctx)
    owner = owner_console(ctx, app)
    before = ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE target='master' AND action='review-request'")["n"]

    def refuse_write(*_args, **_kwargs):
        raise licensing.LicenseError("read-only license")

    monkeypatch.setattr(licensing, "require_writable", refuse_write)
    response = owner.post("/v1/master/review-requests")

    assert response.status_code == 423 and response.json() == {"detail": "read-only license"}
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE target='master' AND action='review-request'")[
        "n"] == before
    assert ctx.db.one("SELECT review_requested_at FROM master_settings WHERE singleton=true")[
        "review_requested_at"] is None


def test_a_database_failure_does_not_report_a_review_request(ctx, monkeypatch):
    app = console_app(ctx)
    owner = owner_console(ctx, app)
    session = owner.cookies.get("aiflow4_session")
    audits_before = ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"]
    original_run = ctx.db.run

    def fail_review_update(sql, *params):
        if "UPDATE master_settings SET review_requested_at=now()" in sql:
            raise RuntimeError("review request database failure")
        return original_run(sql, *params)

    monkeypatch.setattr(ctx.db, "run", fail_review_update)
    with pytest.raises(RuntimeError, match="review request database failure"):
        owner.post("/v1/master/review-requests")

    error_client = TestClient(app, raise_server_exceptions=False)
    error_client.cookies.set("aiflow4_session", session)
    response = error_client.post("/v1/master/review-requests")
    assert response.status_code == 500
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"] == audits_before
    assert ctx.db.one("SELECT review_requested_at FROM master_settings WHERE singleton=true")[
        "review_requested_at"] is None


def test_cycle_metrics_count_idle_ai_and_action_cycles_per_utc_day(ctx):
    insert_master_run(ctx.db, "0 seconds", {"no_watch": True, "model_time_s": 0})
    insert_master_run(ctx.db, "0 seconds", {"completed": 0, "model_time_s": 5})
    insert_master_run(ctx.db, "0 seconds", {"completed": 2, "model_time_s": 9})
    insert_master_run(ctx.db, "1 day", {"no_watch": True, "model_time_s": 0})

    app = console_app(ctx)
    cycles = owner_console(ctx, app).get("/v1/master/autonomy").json()["cycles"]

    assert cycles["target_no_action_percent"] == 5
    assert len(cycles["days"]) == 7
    today, yesterday = cycles["days"][-1], cycles["days"][-2]
    assert {key: today[key] for key in ("cycles", "ai_calls", "actions", "no_action_percent")} == {
        "cycles": 3, "ai_calls": 2, "actions": 1, "no_action_percent": 50.0}
    assert {key: yesterday[key] for key in ("cycles", "ai_calls", "actions", "no_action_percent")} == {
        "cycles": 1, "ai_calls": 0, "actions": 0, "no_action_percent": None}
    assert all(day["cycles"] == 0 for day in cycles["days"][:-2])


def test_no_action_percent_is_the_share_of_ai_calls_without_an_action():
    assert no_action_percent(0, 0) is None
    assert no_action_percent(20, 1) == 95.0
    assert no_action_percent(3, 0) == 100.0
    assert no_action_percent(4, 4) == 0.0
