"""Capacity checks remain usable by independently installed routers."""
from __future__ import annotations

from inspect import Parameter, signature
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aiflow.v4.control import app_helpers, licensing, route_deps
from aiflow.v4.control.db import J


def context(public_key: str) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(license_public_key=public_key))


@pytest.mark.parametrize(("count", "connection"), [(None, None), (0, None), (5, object())])
def test_require_capacity_forwards_context_count_key_and_connection(monkeypatch, count, connection):
    ctx = context("key-at-call-time")
    result = {"status": "active"}
    calls = []

    def require_capacity(received_ctx, resource, received_count, *, public_key, connection=None):
        calls.append((received_ctx, resource, received_count, public_key, connection))
        return result

    monkeypatch.setattr(route_deps.licensing, "require_capacity", require_capacity)

    actual = route_deps.require_capacity(ctx, "projects", count, connection=connection)

    assert actual is result
    received_ctx, resource, received_count, public_key, received_connection = calls[0]
    assert received_ctx is ctx
    assert (resource, received_count, public_key) == ("projects", count, "key-at-call-time")
    assert received_connection is connection


@pytest.mark.parametrize("detail", [
    "Unknown licence limit.",
    "This installation is read-only because its licence is invalid or expired. Activate or renew a licence to continue.",
])
def test_require_capacity_translates_only_license_errors(monkeypatch, detail):
    error = route_deps.licensing.LicenseError(detail)

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(route_deps.licensing, "require_capacity", refuse)

    with pytest.raises(HTTPException) as caught:
        route_deps.require_capacity(context("key"), "projects")

    assert caught.value.status_code == 409
    assert caught.value.detail == detail
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_real_license_refusal_preserves_the_callers_active_transaction(ctx):
    licensing.initialize(ctx.db)
    ctx.settings.license_public_key = "test-public-key"
    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"]["projects"] = 1
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))

    attempted_resource_write = False
    with ctx.db.pool.connection() as connection:
        with pytest.raises(HTTPException) as caught:
            with connection.transaction():
                # A write already owned by the caller must roll back with its transaction.
                connection.execute("INSERT INTO nodes (name) VALUES (%s)", ("capacity-check-transaction-marker",))
                try:
                    route_deps.require_capacity(ctx, "projects", 1, connection=connection)
                except HTTPException:
                    assert connection.info.transaction_status.name == "INTRANS"
                    raise
                attempted_resource_write = True
                connection.execute("INSERT INTO nodes (name) VALUES (%s)", ("capacity-check-resource",))

    assert caught.value.status_code == 409
    assert caught.value.detail.startswith("Projects limit of 1 reached")
    assert not attempted_resource_write
    assert connection.info.transaction_status.name == "IDLE"
    assert ctx.db.one("SELECT name FROM nodes WHERE name IN (%s,%s)",
                      ("capacity-check-transaction-marker", "capacity-check-resource")) is None


def test_external_router_uses_each_apps_current_context_and_translates_refusal(monkeypatch):
    calls = []

    def require_capacity(ctx, resource, count, *, public_key, connection=None):
        calls.append((ctx, resource, count, public_key, connection))
        if public_key == "refuse":
            raise route_deps.licensing.LicenseError("AI accounts limit of 0 reached.")
        return {"resource": resource, "count": count, "key": public_key}

    monkeypatch.setattr(route_deps.licensing, "require_capacity", require_capacity)

    def router_app(ctx):
        app = FastAPI()
        app.state.ctx = ctx
        router = APIRouter()

        @router.get("/capacity")
        def capacity(request: Request, count: int | None = None) -> dict:
            return route_deps.require_capacity(request.app.state.ctx, "ai_accounts", count)

        app.include_router(router)
        return TestClient(app)

    first_ctx, second_ctx = context("first-key"), context("second-key")
    first, second = router_app(first_ctx), router_app(second_ctx)
    assert first.get("/capacity", params={"count": 0}).json() == {
        "resource": "ai_accounts", "count": 0, "key": "first-key"}
    assert second.get("/capacity").json() == {
        "resource": "ai_accounts", "count": None, "key": "second-key"}

    first_ctx.settings.license_public_key = "refuse"
    refused = first.get("/capacity")
    assert refused.status_code == 409
    assert refused.json() == {"detail": "AI accounts limit of 0 reached."}
    assert [call[0] for call in calls] == [first_ctx, second_ctx, first_ctx]
    assert [call[3] for call in calls] == ["first-key", "second-key", "refuse"]
    first.close()
    second.close()


def test_non_license_exception_propagates_and_next_call_uses_updated_context(monkeypatch):
    ctx = context("old-key")
    failure = RuntimeError("database unavailable")
    keys = []

    def require_capacity(_ctx, _resource, _count, *, public_key, connection=None):
        keys.append(public_key)
        if len(keys) == 1:
            raise failure
        return {"status": "active"}

    monkeypatch.setattr(route_deps.licensing, "require_capacity", require_capacity)
    with pytest.raises(RuntimeError) as caught:
        route_deps.require_capacity(ctx, "projects")
    assert caught.value is failure

    ctx.settings.license_public_key = "new-key"
    assert route_deps.require_capacity(ctx, "projects") == {"status": "active"}
    assert keys == ["old-key", "new-key"]


def test_installed_capacity_closure_keeps_signature_and_delegates(monkeypatch, ctx):
    result = {"status": "active"}
    calls = []

    def require_capacity(received_ctx, resource, count=None, *, connection=None):
        calls.append((received_ctx, resource, count, connection))
        return result

    monkeypatch.setattr(route_deps, "require_capacity", require_capacity)
    helpers = app_helpers.install(FastAPI(), ctx, SimpleNamespace(late=SimpleNamespace()))

    parameters = signature(helpers.require_capacity).parameters
    assert tuple(parameters) == ("resource", "count", "connection")
    assert parameters["count"].default is None
    assert parameters["connection"].kind is Parameter.KEYWORD_ONLY
    assert parameters["connection"].default is None
    connection = object()
    assert helpers.require_capacity("projects", 0, connection=connection) is result
    received_ctx, resource, count, received_connection = calls[0]
    assert received_ctx is ctx
    assert (resource, count) == ("projects", 0)
    assert received_connection is connection
