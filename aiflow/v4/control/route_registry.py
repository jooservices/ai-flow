"""Discover and mount feature-owned control API routers."""
from __future__ import annotations

import importlib
import pkgutil
from fastapi import APIRouter, FastAPI


DEFAULT_PRIORITY = 100
ROUTES_PACKAGE = "aiflow.v4.control.routes"


def discover_routers(package_name: str = ROUTES_PACKAGE) -> list[tuple[int, str, APIRouter]]:
    """Import each feature module and return its router in stable priority order."""
    package = importlib.import_module(package_name)
    locations = getattr(package, "__path__", None)
    if locations is None:
        raise TypeError(f"{package_name} is not a package")

    routers = []
    for entry in pkgutil.iter_modules(locations, package.__name__ + "."):
        if entry.ispkg:
            continue
        module = importlib.import_module(entry.name)
        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            raise TypeError(f"{entry.name} must expose a FastAPI APIRouter named 'router'")
        priority = getattr(module, "priority", DEFAULT_PRIORITY)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TypeError(f"{entry.name}.priority must be a whole-number integer")
        routers.append((priority, module.__name__, router))
    return sorted(routers, key=lambda item: (item[0], item[1]))


def mount_discovered_routers(app: FastAPI, package_name: str = ROUTES_PACKAGE) -> None:
    """Mount all routers discovered under the feature route package."""
    for _priority, _module_name, router in discover_routers(package_name):
        app.include_router(router)
