"""Protect assembled escalation and decision route registrations from duplicates."""
from __future__ import annotations

import pytest
from fastapi import FastAPI

from aiflow.v4.control import route_registry
from aiflow.v4.control.api import create_app
from aiflow.v4.control.routes import decisions, escalations


ROUTE_OWNERS = (
    ("GET", "/v1/escalations", "aiflow.v4.control.routes.escalations"),
    ("POST", "/v1/escalations/raise", "aiflow.v4.control.routes.escalations"),
    ("POST", "/v1/escalations/triage", "aiflow.v4.control.routes.escalations"),
    ("POST", "/v1/decisions", "aiflow.v4.control.routes.decisions"),
    ("POST", "/v1/decisions/{decision_id}/answer", "aiflow.v4.control.routes.decisions"),
    ("POST", "/v1/decisions/{decision_id}/retire", "aiflow.v4.control.routes.decisions"),
)


def _assert_one_route(routes, method: str, path: str, module: str) -> None:
    matches = [route for route in route_registry.concrete_routes(routes)
               if route.path == path and method in (getattr(route, "methods", ()) or ())]
    assert len(matches) == 1, f"expected one {method} {path}, found {len(matches)}"
    assert matches[0].endpoint.__module__ == module


def test_assembled_app_registers_each_original_route_once_with_expected_owner(ctx):
    app = create_app(ctx)

    for method, path, module in ROUTE_OWNERS:
        _assert_one_route(app.routes, method, path, module)


def test_discovery_returns_each_expected_module_router_once():
    expected = (
        ("aiflow.v4.control.routes.escalations", escalations.router),
        ("aiflow.v4.control.routes.decisions", decisions.router),
    )
    discovered = route_registry.discover_routers()

    for module, router in expected:
        matches = [(name, found_router) for _priority, name, found_router in discovered if name == module]
        assert len(matches) == 1
        assert matches[0][1] is router


def test_duplicate_pair_fails_without_counting_other_methods_or_paths():
    app = FastAPI()

    def target():
        return None

    def other_method():
        return None

    def other_path():
        return None

    def duplicate_target():
        return None

    app.add_api_route("/target", target, methods=["GET"])
    app.add_api_route("/target", other_method, methods=["POST"])
    app.add_api_route("/unrelated", other_path, methods=["GET"])
    _assert_one_route(app.routes, "GET", "/target", __name__)

    app.add_api_route("/target", duplicate_target, methods=["GET"])
    with pytest.raises(AssertionError, match="expected one GET /target, found 2"):
        _assert_one_route(app.routes, "GET", "/target", __name__)
