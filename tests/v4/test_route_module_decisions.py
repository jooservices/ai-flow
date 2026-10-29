"""The owner decision routes are served from their feature module (JAIF-398)."""
from __future__ import annotations

from aiflow.v4.control import api
from aiflow.v4.control.routes import decisions

PATHS = {("POST", "/v1/decisions"), ("POST", "/v1/decisions/{decision_id}/answer"),
         ("POST", "/v1/decisions/{decision_id}/retire")}


def test_the_module_defines_exactly_the_three_decision_routes():
    assert {(m, r.path) for r in decisions.router.routes for m in r.methods} == PATHS


def test_the_app_serves_them_from_the_module_and_api_py_no_longer_defines_them(ctx):
    served = {}
    for route in api.create_app(ctx).routes:
        inner = getattr(route, "original_router", None)
        for r in (inner.routes if inner else [route]):
            for method in getattr(r, "methods", ()) or ():
                if (method, getattr(r, "path", "")) in PATHS:
                    served[(method, r.path)] = r.endpoint.__module__
    assert set(served) == PATHS and set(served.values()) == {"aiflow.v4.control.routes.decisions"}


def test_a_decision_needs_an_owner_session(client):
    assert client.post("/v1/decisions", json={"title": "Some title", "decision": "Some decision"}).status_code in (401, 503)
