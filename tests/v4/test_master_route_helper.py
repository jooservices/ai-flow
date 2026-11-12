"""The app master-run check is reusable from routers without changing its refusals."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aiflow.v4.control import app_helpers, route_deps


def installed_helpers(ctx):
    return app_helpers.install(FastAPI(), ctx, SimpleNamespace(late=SimpleNamespace()))


def add_master_run(ctx, *, step="master", node="node1"):
    run_id = str(uuid.uuid4())
    ticket = "TESTWP-428"
    claim = ctx.leases.claim(ticket, run_id, ctx.settings.lease_ms)
    assert claim.ok
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status)"
               " VALUES (%s,%s,'TESTWP',%s,'claude-01','claude',%s,%s,'running')",
               (run_id, ticket, step, node, claim.fence))
    return run_id, claim.fence


def test_installed_master_run_forwards_action_and_payload_unchanged(monkeypatch):
    ctx = SimpleNamespace()
    helpers = installed_helpers(ctx)
    run = {"step": "master", "node": "node1"}
    received = []
    cases = [
        ("context", None),
        ("empty-payload", {}),
        ("inspect", {"unchanged": [1, 2, 3]}),
        ("large-payload", {"blob": "x" * 8192, "items": list(range(500))}),
    ]

    def fenced(got_ctx, run_id, fence, target, action, payload):
        received.append((got_ctx, run_id, fence, target, action, payload))
        return run

    monkeypatch.setattr(route_deps, "fenced", fenced)
    for action, payload in cases:
        result = helpers.master_run("run-1", 7, "node1", action, payload)
        assert result is run
    assert [(call[0], call[1], call[2], call[3], call[4]) for call in received] == [
        (ctx, "run-1", 7, "master", action) for action, _payload in cases]
    assert [call[5] for call in received] == [payload for _action, payload in cases]
    assert all(call[5] is payload for call, (_action, payload) in zip(received, cases, strict=True))


def test_installed_master_run_delegates_to_context_helper(monkeypatch):
    ctx = SimpleNamespace()
    helpers = installed_helpers(ctx)
    expected = {"run": "same"}
    calls = []

    def master_run(*args):
        calls.append(args)
        return expected

    monkeypatch.setattr(route_deps, "master_run", master_run)
    payload = {"intent": "inspect"}

    assert helpers.master_run("run-2", 8, "node2", "context", payload) is expected
    assert calls == [(ctx, "run-2", 8, "node2", "context", payload)]


def test_installed_master_run_returns_the_active_run(ctx):
    run_id, fence = add_master_run(ctx)
    run = ctx.db.one("SELECT * FROM runs WHERE id=%s", (run_id,))

    assert installed_helpers(ctx).master_run(run_id, fence, "node1", "context") == run


def test_independent_router_uses_the_application_context_for_master_run(ctx):
    run_id, fence = add_master_run(ctx)
    app = FastAPI()
    app.state.ctx = ctx
    router = APIRouter()

    @router.get("/master-probe/{run_id}")
    def master_probe(request: Request, run_id: str, fence: int, node: str, action: str) -> dict:
        return route_deps.master_run(request.app.state.ctx, run_id, fence, node, action, {"probe": True})

    app.include_router(router)
    client = TestClient(app)
    expected = installed_helpers(ctx).master_run(run_id, fence, "node1", "context")
    response = client.get(f"/master-probe/{run_id}", params={"fence": fence, "node": "node1", "action": "context"})

    assert response.status_code == 200
    returned = response.json()
    assert {key: returned[key] for key in ("ticket_key", "step", "node", "fence", "status")} == {
        key: expected[key] for key in ("ticket_key", "step", "node", "fence", "status")}
    assert returned["id"] == str(expected["id"])


def test_independent_router_preserves_master_run_errors(ctx):
    run_id, fence = add_master_run(ctx, node="node1")
    app = FastAPI()
    app.state.ctx = ctx
    router = APIRouter()

    @router.get("/master-probe/{run_id}")
    def master_probe(request: Request, run_id: str, fence: int, node: str) -> dict:
        return route_deps.master_run(request.app.state.ctx, run_id, fence, node, "context")

    app.include_router(router)
    client = TestClient(app)
    rejected = client.get(f"/master-probe/{run_id}", params={"fence": fence, "node": "other-node"})
    missing = client.get(f"/master-probe/{uuid.uuid4()}", params={"fence": fence, "node": "node1"})
    stale = client.get(f"/master-probe/{run_id}", params={"fence": fence + 1, "node": "node1"})

    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "master run belongs to another node"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "unknown run"}
    assert stale.status_code == 409
    assert stale.json() == {"detail": "fencing token is no longer valid"}
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE result='stale fencing token'") == {"n": 1}
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='write.refused'") == {"n": 1}


def test_installed_master_run_rejects_malformed_and_unknown_runs_without_side_effects(ctx, monkeypatch):
    helpers = installed_helpers(ctx)
    validations = []
    monkeypatch.setattr(ctx.leases, "valid", lambda *args: validations.append(args) or True)

    for run_id in ("not-a-uuid", str(uuid.uuid4())):
        with pytest.raises(HTTPException) as caught:
            helpers.master_run(run_id, 1, "node1", "context")
        assert caught.value.status_code == 404
        assert caught.value.detail == "unknown run"

    assert validations == []
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit") == {"n": 0}
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='write.refused'") == {"n": 0}


@pytest.mark.parametrize("lease_state", ["wrong_fence", "expired_lease"])
def test_installed_master_run_fencing_refusal_audits_and_emits_once(ctx, lease_state):
    run_id, fence = add_master_run(ctx)
    passed_fence = fence + 1 if lease_state == "wrong_fence" else fence
    if lease_state == "expired_lease":
        assert ctx.leases.release("TESTWP-428", run_id, fence)

    with pytest.raises(HTTPException) as caught:
        installed_helpers(ctx).master_run(run_id, passed_fence, "other-node", "context", {"x": 1})

    assert caught.value.status_code == 409
    assert caught.value.detail == "fencing token is no longer valid"
    assert ctx.db.all("SELECT target,action,accepted,result,payload FROM write_audit") == [
        {"target": "master", "action": "context", "accepted": False,
         "result": "stale fencing token", "payload": {"x": 1}},
    ]
    assert ctx.db.all("SELECT type,reason FROM events WHERE type='write.refused'") == [
        {"type": "write.refused", "reason": "master.context: stale fence"},
    ]


def test_stale_fence_precedes_step_and_node_refusals(ctx):
    run_id, fence = add_master_run(ctx, step="dev")

    with pytest.raises(HTTPException) as caught:
        installed_helpers(ctx).master_run(run_id, fence + 1, "other-node", "context")

    assert caught.value.status_code == 409
    assert caught.value.detail == "fencing token is no longer valid"
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE accepted=false") == {"n": 1}
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='write.refused'") == {"n": 1}


@pytest.mark.parametrize(("step", "node", "detail"), [
    ("dev", "node1", "only a master run can use this endpoint"),
    ("master", "other-node", "master run belongs to another node"),
    ("dev", "other-node", "only a master run can use this endpoint"),
])
def test_installed_master_run_preserves_step_then_node_refusal(ctx, step, node, detail):
    run_id, fence = add_master_run(ctx, step=step)

    with pytest.raises(HTTPException) as caught:
        installed_helpers(ctx).master_run(run_id, fence, node, "context")

    assert caught.value.status_code == 403
    assert caught.value.detail == detail
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit") == {"n": 0}
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='write.refused'") == {"n": 0}
