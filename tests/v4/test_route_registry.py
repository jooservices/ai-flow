from __future__ import annotations

import importlib
import uuid

import pytest
from fastapi import APIRouter, FastAPI

from aiflow.v4.control import route_registry
from aiflow.v4.control.api import create_app
from aiflow.v4.control.route_registry import DEFAULT_PRIORITY, discover_routers, mount_discovered_routers


def _route_package(tmp_path, monkeypatch):
    package_name = f"test_feature_routes_{uuid.uuid4().hex}"
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text('"""Synthetic routes package for discovery tests."""\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return package_name, package


def _write_feature(package, name, path, priority=None):
    source = "from fastapi import APIRouter\nrouter = APIRouter()\n"
    if priority is not None:
        source += f"priority = {priority!r}\n"
    source += f"@router.get({path!r})\ndef endpoint():\n    return {{'feature': {name!r}}}\n"
    (package / f"{name}.py").write_text(source)
    importlib.invalidate_caches()


def test_discovery_mounts_routers_by_priority_then_module_name_with_default_priority(tmp_path, monkeypatch):
    package_name, package = _route_package(tmp_path, monkeypatch)
    _write_feature(package, "zeta", "/zeta", 50)
    _write_feature(package, "alpha", "/alpha", 50)
    _write_feature(package, "early", "/early", 10)
    _write_feature(package, "defaulted", "/defaulted")

    found = discover_routers(package_name)
    assert [name.rsplit(".", 1)[-1] for _priority, name, _router in found] == [
        "early", "alpha", "zeta", "defaulted",
    ]
    assert [priority for priority, _name, _router in found] == [10, 50, 50, DEFAULT_PRIORITY]

    app = FastAPI()
    mount_discovered_routers(app, package_name)
    mounted = list(app.openapi()["paths"])
    assert mounted == ["/early", "/alpha", "/zeta", "/defaulted"]


def test_adding_a_feature_module_requires_no_registration_list(tmp_path, monkeypatch):
    package_name, package = _route_package(tmp_path, monkeypatch)
    _write_feature(package, "first", "/first")
    _write_feature(package, "new_feature", "/new-feature", 25)

    app = FastAPI()
    mount_discovered_routers(app, package_name)

    assert "/new-feature" in app.openapi()["paths"]


def test_app_creation_mounts_routers_from_the_registry(ctx, monkeypatch):
    router = APIRouter()

    @router.get("/startup-discovered")
    def endpoint():
        return {"ready": True}

    discovered = []
    monkeypatch.setattr(route_registry, "discover_routers", lambda package_name: (
        discovered.append(package_name) or [(DEFAULT_PRIORITY, "feature", router)]))

    app = create_app(ctx)

    assert discovered == [route_registry.ROUTES_PACKAGE]
    assert "/startup-discovered" in app.openapi()["paths"]


def test_priority_must_be_a_whole_number(tmp_path, monkeypatch):
    package_name, package = _route_package(tmp_path, monkeypatch)
    _write_feature(package, "invalid", "/invalid", 1.5)

    with pytest.raises(TypeError, match="whole-number integer"):
        discover_routers(package_name)
