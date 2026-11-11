from __future__ import annotations

import copy
import uuid

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control import api, route_deps, run_inbox
from aiflow.v4.control.route_registry import concrete_routes
from conftest import add_ticket, join_node


HEARTBEAT_PATH = "/v1/runs/{run_id}/heartbeat"


def _heartbeat_routes(app):
    return [route for route in concrete_routes(app.routes)
            if route.path == HEARTBEAT_PATH and "POST" in (route.methods or set())]


def _claim_heartbeat(client, ctx, key: str):
    joined = join_node(client, "cc")
    node = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    add_ticket(ctx.db, key)
    claimed = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01"}, headers=node)
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    return node, claimed.json()


def test_each_app_heartbeat_uses_its_own_context(ctx, monkeypatch):
    second_context = copy.copy(ctx)
    assert second_context is not ctx
    seen_contexts = []

    monkeypatch.setattr(route_deps, "node_auth", lambda _ctx, _authorization="": "node-a")
    monkeypatch.setattr(route_deps, "run_row", lambda _ctx, _run_id: {"node": "node-a"})

    def heartbeat(request_context, run, run_id, body, node):
        seen_contexts.append(request_context)
        return {"ok": True}

    monkeypatch.setattr(run_inbox, "heartbeat", heartbeat)
    first_app = api.create_app(ctx)
    second_app = api.create_app(second_context)
    assert len(_heartbeat_routes(first_app)) == 1
    assert len(_heartbeat_routes(second_app)) == 1

    body = {"fence": 1}
    headers = {"Authorization": "Bearer characterization-token"}
    for app in (first_app, second_app):
        response = TestClient(app).post(
            HEARTBEAT_PATH.format(run_id=uuid.uuid4()), json=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    assert len(seen_contexts) == 2
    assert seen_contexts[0] is ctx
    assert seen_contexts[1] is second_context


def test_discovered_router_serves_the_heartbeat_once(ctx):
    app = api.create_app(ctx)

    routes = _heartbeat_routes(app)

    assert len(routes) == 1
    assert routes[0].endpoint.__module__ == "aiflow.v4.control.routes.run_heartbeat"


def test_heartbeat_saves_progress_and_renews_the_run_lease(client, ctx, monkeypatch):
    node, run = _claim_heartbeat(client, ctx, "TESTWP-HEARTBEAT-PROGRESS")
    renew = ctx.leases.renew
    renewals = []

    def record_renewal(ticket, run_id, fence, ttl_ms):
        result = renew(ticket, run_id, fence, ttl_ms)
        renewals.append((ticket, run_id, fence, ttl_ms, result))
        return result

    monkeypatch.setattr(ctx.leases, "renew", record_renewal)
    before = ctx.db.one("SELECT heartbeat_at FROM runs WHERE id=%s", (run["run_id"],))["heartbeat_at"]

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"], "progress": {"phase": "verify", "percent": 40}},
                           headers=node)

    assert response.status_code == 200
    assert response.json()["lease_ms"] == ctx.settings.lease_ms
    assert renewals == [("TESTWP-HEARTBEAT-PROGRESS", run["run_id"], run["fence"],
                         ctx.settings.lease_ms, True)]
    saved = ctx.db.one("SELECT heartbeat_at,progress FROM runs WHERE id=%s", (run["run_id"],))
    assert saved["heartbeat_at"] is not None and saved["heartbeat_at"] >= before
    assert saved["progress"]["phase"] == "verify" and saved["progress"]["percent"] == 40
    assert "reported_at" in saved["progress"]


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-node-token"])
def test_heartbeat_requires_a_valid_node_token(client, ctx, authorization):
    _node, run = _claim_heartbeat(client, ctx, "TESTWP-HEARTBEAT-AUTH")
    headers = {} if authorization is None else {"Authorization": authorization}

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat", json={"fence": run["fence"]}, headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize("run_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_heartbeat_rejects_malformed_and_unknown_run_ids(client, ctx, run_id):
    node, _run = _claim_heartbeat(client, ctx, "TESTWP-HEARTBEAT-UNKNOWN")

    response = client.post(f"/v1/runs/{run_id}/heartbeat", json={"fence": 1}, headers=node)

    assert response.status_code == 404


def test_heartbeat_rejects_another_node_and_a_non_running_run(client, ctx):
    node, run = _claim_heartbeat(client, ctx, "TESTWP-HEARTBEAT-OWNER")
    other = join_node(client, "other-worker")
    wrong_node = {"Authorization": f"Bearer {other.json()['node_token']}"}
    denied = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                         json={"fence": run["fence"]}, headers=wrong_node)
    assert denied.status_code == 403

    finished = client.post(f"/v1/runs/{run['run_id']}/result",
                           json={"fence": run["fence"], "outcome": "done"}, headers=node)
    assert finished.status_code == 200
    inactive = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"]}, headers=node)
    assert inactive.status_code == 409


@pytest.mark.parametrize("field", ["inbox_received", "inbox_included"])
@pytest.mark.parametrize(("size", "status"), [(100, 200), (101, 422)])
def test_heartbeat_inbox_fields_enforce_the_100_item_boundary(client, ctx, field, size, status):
    node, run = _claim_heartbeat(client, ctx, f"TESTWP-HEARTBEAT-{field}-{size}")
    body = {"fence": run["fence"], field: list(range(size))}

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat", json=body, headers=node)

    assert response.status_code == status


@pytest.mark.parametrize(("body", "error_type"), [({}, "missing"), ({"fence": "invalid"}, "int_parsing")])
def test_heartbeat_rejects_malformed_body_with_validation_error(client, ctx, body, error_type):
    node, run = _claim_heartbeat(client, ctx, "TESTWP-HEARTBEAT-MALFORMED-BODY")

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat", json=body, headers=node)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "fence"]
    assert response.json()["detail"][0]["type"] == error_type


def test_stale_fence_loses_the_run_and_closes_pending_inbox(client, ctx):
    node, run = _claim_heartbeat(client, ctx, "TESTWP-HEARTBEAT-STALE-FENCE")
    queued = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "context", "body": "handoff"})
    assert queued.status_code == 201
    message_id = queued.json()["message"]["id"]

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"] + 1}, headers=node)

    assert response.status_code == 409
    saved = ctx.db.one("SELECT status,inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))
    assert saved == {"status": "lost", "inbox_pending_count": 0}
    history = client.get(f"/v1/runs/{run['run_id']}/inbox").json()["messages"]
    assert [(message["id"], message["status"]) for message in history] == [(message_id, "undelivered")]
    event_types = ctx.db.all("SELECT type FROM events WHERE run_id=%s ORDER BY id", (run["run_id"],))
    assert [event["type"] for event in event_types if event["type"] in
            ("run.inbox.undelivered", "run.lost")] == ["run.inbox.undelivered", "run.lost"]


def test_lost_lease_heartbeat_cleans_up_pending_inbox_and_emits_events(client, ctx):
    node, run = _claim_heartbeat(client, ctx, "TESTWP-HEARTBEAT-LOST-LEASE")
    queued = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "context", "body": "handoff"})
    assert queued.status_code == 201
    message_id = queued.json()["message"]["id"]
    assert ctx.leases.release("TESTWP-HEARTBEAT-LOST-LEASE", run["run_id"], run["fence"])

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"]}, headers=node)

    assert response.status_code == 409
    saved = ctx.db.one("SELECT status,inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))
    assert saved == {"status": "lost", "inbox_pending_count": 0}
    history = client.get(f"/v1/runs/{run['run_id']}/inbox").json()["messages"]
    assert [(message["id"], message["status"]) for message in history] == [(message_id, "undelivered")]
    event_types = ctx.db.all("SELECT type FROM events WHERE run_id=%s ORDER BY id", (run["run_id"],))
    assert [event["type"] for event in event_types if event["type"] in
            ("run.inbox.undelivered", "run.lost")] == ["run.inbox.undelivered", "run.lost"]
