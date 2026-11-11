from __future__ import annotations

import copy
import uuid

from fastapi.testclient import TestClient

from aiflow.v4.control import api, route_deps, run_inbox
from aiflow.v4.control.route_registry import concrete_routes


HEARTBEAT_PATH = "/v1/runs/{run_id}/heartbeat"


def _heartbeat_routes(app):
    return [route for route in concrete_routes(app.routes)
            if route.path == HEARTBEAT_PATH and "POST" in (route.methods or set())]


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
