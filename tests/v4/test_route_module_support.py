"""The Support routes are served from their feature module (JAIF-390)."""
from __future__ import annotations

from aiflow.v4.control import api
from aiflow.v4.control.routes import support

PATHS = {("POST", "/v1/support/bundles"),
         ("GET", "/v1/support/bundles/{bundle_id}/download")}


def test_the_module_defines_exactly_these_routes():
    assert {(m, r.path) for r in support.router.routes for m in r.methods if m != "HEAD"} == PATHS


def test_the_app_serves_them_from_the_module(ctx):
    served = {}
    for route in api.create_app(ctx).routes:
        inner = getattr(route, "original_router", None)
        for r in (inner.routes if inner else [route]):
            for method in getattr(r, "methods", ()) or ():
                if (method, getattr(r, "path", "")) in PATHS:
                    served[(method, r.path)] = r.endpoint.__module__
    assert set(served) == PATHS and set(served.values()) == {"aiflow.v4.control.routes.support"}
