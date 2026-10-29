"""The dashboard read routes are served from their feature module (JAIF-401)."""
from __future__ import annotations

from aiflow.v4.control import api
from aiflow.v4.control.routes import dashboard

PATHS = {("GET", "/v1/dashboard/waiting"), ("GET", "/v1/dashboard/disagreements"), ("GET", "/v1/dashboard/quota")}


def test_the_module_defines_exactly_the_three_dashboard_routes():
    assert {(m, r.path) for r in dashboard.router.routes for m in r.methods if m != "HEAD"} == PATHS


def test_the_app_serves_them_from_the_module(ctx):
    served = {}
    for route in api.create_app(ctx).routes:
        inner = getattr(route, "original_router", None)
        for r in (inner.routes if inner else [route]):
            for method in getattr(r, "methods", ()) or ():
                if (method, getattr(r, "path", "")) in PATHS:
                    served[(method, r.path)] = r.endpoint.__module__
    assert set(served) == PATHS and set(served.values()) == {"aiflow.v4.control.routes.dashboard"}


def test_the_review_queue_is_reachable_for_routers_through_the_app_state(ctx):
    assert api.create_app(ctx).state.review_queue is not None


def test_the_three_routes_answer(client):
    for _, path in sorted(PATHS):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
