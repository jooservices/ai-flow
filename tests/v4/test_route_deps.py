"""Shared route helpers outside create_app (JAIF-421)."""
from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from aiflow.v4.control import route_deps


def node(ctx, name="node1", token="node-token-1"):
    ctx.db.run("INSERT INTO nodes (name, role, status, token_hash) VALUES (%s,'worker','online',%s)"
               " ON CONFLICT (name) DO UPDATE SET token_hash = EXCLUDED.token_hash",
               (name, hashlib.sha256(token.encode()).hexdigest()))
    return token


def test_node_auth_accepts_a_known_token_and_refuses_the_rest(ctx):
    token = node(ctx)
    assert route_deps.node_auth(ctx, f"Bearer {token}") == "node1"
    for bad in ("", "Bearer nope", "Bearer "):
        with pytest.raises(HTTPException) as caught:
            route_deps.node_auth(ctx, bad)
        assert caught.value.status_code == 401 and caught.value.detail == "unknown node token"


def test_a_router_outside_api_py_can_use_the_node_dependency(ctx):
    token = node(ctx)
    app = FastAPI()
    app.state.ctx = ctx
    router = APIRouter()

    @router.get("/probe")
    def probe(name: str = Depends(route_deps.node_auth_dependency)) -> dict:
        return {"node": name}

    app.include_router(router)
    client = TestClient(app)
    assert client.get("/probe").status_code == 401
    ok = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and ok.json() == {"node": "node1"}


def add_run(ctx, fence=1):
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status)"
               " VALUES (%s,'TESTWP-1','TESTWP','dev','claude-01','claude','node1',%s,'running')", (run_id, fence))
    return run_id


def test_run_row_refuses_unknown_and_malformed_ids(ctx):
    for bad in ("not-a-uuid", str(uuid.uuid4())):
        with pytest.raises(HTTPException) as caught:
            route_deps.run_row(ctx, bad)
        assert caught.value.status_code == 404 and caught.value.detail == "unknown run"
    run_id = add_run(ctx)
    assert route_deps.run_row(ctx, run_id)["ticket_key"] == "TESTWP-1"


def test_fenced_refuses_a_stale_token_and_records_the_refusal(ctx):
    run_id = add_run(ctx, fence=3)
    with pytest.raises(HTTPException) as caught:
        route_deps.fenced(ctx, run_id, 3, "github", "comment", {"number": 1})
    assert caught.value.status_code == 409 and caught.value.detail == "fencing token is no longer valid"
    row = ctx.db.one("SELECT accepted, result, target, action FROM write_audit ORDER BY id DESC LIMIT 1")
    assert row == {"accepted": False, "result": "stale fencing token", "target": "github", "action": "comment"}


def test_audit_writes_a_row_and_names_the_github_connection(ctx):
    token = route_deps.github_connection_used.set("conn-1")
    try:
        route_deps.audit(ctx, "github", "comment", "TESTWP-1", "", None, "claude-01", True, "ok", {"number": 7})
        route_deps.audit(ctx, "jira", "comment", "TESTWP-1", "", None, "claude-01", True, "ok", {})
    finally:
        route_deps.github_connection_used.reset(token)
    rows = ctx.db.all("SELECT target, payload FROM write_audit ORDER BY id DESC LIMIT 2")
    assert rows[1]["payload"] == {"number": 7, "connection": "conn-1"} and rows[0]["payload"] == {}


def test_console_user_needs_a_configured_signing_key_and_a_session(ctx, client):
    ctx.settings.console_session_secret = "short"
    request = client.build_request("GET", "/")
    with pytest.raises(HTTPException) as caught:
        route_deps.console_user(ctx, type("Req", (), {"cookies": {}})(), "console.read", "t", "write")
    assert caught.value.status_code == 503
    assert ctx.db.one("SELECT accepted, result FROM write_audit ORDER BY id DESC LIMIT 1") == {
        "accepted": False, "result": "console session signing key is not configured"}
    ctx.settings.console_session_secret = "test-console-session-secret-that-is-long-enough"
    with pytest.raises(HTTPException) as caught:
        route_deps.console_user(ctx, type("Req", (), {"cookies": {}})(), "console.read")
    assert caught.value.status_code == 401
    assert route_deps.optional_console_user(ctx, type("Req", (), {"cookies": {}})()) is None
