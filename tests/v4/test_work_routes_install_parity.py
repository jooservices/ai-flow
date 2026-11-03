"""Characterize the work route registrations before they are split into installer groups."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control import api
from aiflow.v4.control.features.work_routes import routes
from aiflow.v4.control.features.work_routes.routes import install


FIXTURE = Path(__file__).with_name("fixtures") / "work_routes-install-parity.jsonl"


class FakeNodeAuth:
    def __call__(self):
        return "node"

    def __repr__(self):
        return "FakeNodeAuth"


class RecordingApp:
    """Record FastAPI-style route decorators without mounting handlers."""

    def __init__(self):
        self.registrations = []

    def _decorator(self, method, path, kwargs):
        def register(handler):
            self.registrations.append(
                {
                    "method": method,
                    "path": path,
                    "handler": handler.__name__,
                    "signature": str(inspect.signature(handler)),
                    "kwargs": kwargs,
                }
            )
            return handler

        return register

    def get(self, path, **kwargs):
        return self._decorator("GET", path, kwargs)

    def post(self, path, **kwargs):
        return self._decorator("POST", path, kwargs)

    def put(self, path, **kwargs):
        return self._decorator("PUT", path, kwargs)

    def patch(self, path, **kwargs):
        return self._decorator("PATCH", path, kwargs)

    def delete(self, path, **kwargs):
        return self._decorator("DELETE", path, kwargs)

    def api_route(self, path, **kwargs):
        return self._decorator("API_ROUTE", path, kwargs)

    def middleware(self, middleware_type):
        return self._decorator("MIDDLEWARE", middleware_type, {})

    def on_event(self, event_type):
        return self._decorator("ON_EVENT", event_type, {})


def _install_with_recording_app():
    app = RecordingApp()
    deps = SimpleNamespace(
        account_master_paused=lambda _account: False,
        account_quota_at_risk=lambda *_args: False,
        bot_identity=lambda *_args: {},
        capacity_history=lambda: [],
        master_budget_state=lambda: {"enabled": True},
        node_auth=FakeNodeAuth(),
        projects_routes=SimpleNamespace(ticket_epic=lambda _ticket: None),
        quota_forecast_at_risk=lambda *_args: False,
        quota_peer_accounts=lambda *_args: [],
    )
    returned = install(app, SimpleNamespace(), deps)
    return app, returned


def test_install_registrations_match_the_unchanged_golden():
    app, _ = _install_with_recording_app()
    actual = [json.dumps(item, separators=(",", ":")) for item in app.registrations]
    assert actual == FIXTURE.read_text(encoding="utf-8").splitlines()


def test_install_returns_the_same_work_helpers():
    _, returned = _install_with_recording_app()
    assert set(vars(returned)) == {
        "last_dev",
        "eligible",
        "route_capacity",
        "work_deps",
        "master_ready",
        "master_candidate",
        "entitled_candidates",
        "related_hits",
        "claim",
    }
    assert all(callable(getattr(returned, name)) for name in vars(returned) if name != "work_deps")


def test_eligible_account_helper_uses_offers_for_the_current_machine():
    account = SimpleNamespace(id="claude-01", machines=["node-a"], tier="team")
    runtime = SimpleNamespace(ctx=SimpleNamespace(repo=lambda: SimpleNamespace(accounts=[account])))
    work_deps = SimpleNamespace(eligible=lambda *_args, **_kwargs: None)
    offered = [{"key": "SHOP-1", "step": "dev"}]

    actual = routes._eligible_by_account(
        runtime, work_deps, offered, {"claude-01": {"id": "claude-01"}}, "claude-01", "node-a", None
    )

    assert actual == {("SHOP-1", "dev"): {"claude-01": {"id": "claude-01", "tier": "team"}}}


def test_releases_reader_uses_the_control_api_value_at_call_time(monkeypatch):
    releases = Path("test-releases")
    monkeypatch.setattr(api, "RELEASES", releases)
    assert routes._releases() == releases
    monkeypatch.setattr(routes, "_releases", lambda: "latest")
    assert routes._route_releases() == "latest"


def test_last_dev_preserves_query_and_done_filter():
    calls = []
    expected = {"id": "run-1"}
    ctx = SimpleNamespace(db=SimpleNamespace(one=lambda query, params: calls.append((query, params)) or expected))

    assert routes._last_dev(ctx, "SHOP-1", done=True) == expected
    assert calls == [
        (
            "SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
            " AND outcome='done' ORDER BY started_at DESC LIMIT 1",
            ("SHOP-1",),
        )
    ]


def test_eligible_helper_passes_runtime_and_options_to_pipeline(monkeypatch):
    expected = [{"key": "SHOP-1"}]
    calls = []
    monkeypatch.setattr(routes, "eligible_tickets", lambda *args, **kwargs: calls.append((args, kwargs)) or expected)
    runtime = SimpleNamespace(ctx=object())
    work_deps = object()

    assert routes._eligible(runtime, work_deps, "claude-01", "node-a", ticket_key="SHOP-1") == expected
    assert calls == [((runtime.ctx, work_deps, "claude-01", "node-a"), {"ticket_key": "SHOP-1"})]


def test_route_capacity_returns_non_development_offers_without_database_reads():
    offered = [{"key": "SHOP-2", "step": "verify"}]
    runtime = SimpleNamespace(ctx=object())

    assert routes._route_capacity(runtime, object(), "claude-01", "node-a", offered) is offered


def test_master_ready_stops_before_database_when_account_is_paused():
    runtime = SimpleNamespace(
        ctx=SimpleNamespace(leases=SimpleNamespace(cooling=lambda _account: False)),
        account_master_paused=lambda _account: True,
    )

    assert routes._master_ready(runtime, "claude-01", "node-a") is False


def test_master_candidate_preserves_missing_account_refusal():
    runtime = SimpleNamespace(ctx=SimpleNamespace(repo=lambda: SimpleNamespace(account=lambda _id: None)))

    with pytest.raises(routes.HTTPException) as exc_info:
        routes._master_candidate(runtime, "missing", "node-a")
    assert (exc_info.value.status_code, exc_info.value.detail) == (403, "account missing does not run on node-a")


def test_entitled_candidates_keeps_empty_offer_result():
    runtime = SimpleNamespace(ctx=SimpleNamespace(repo=lambda: SimpleNamespace(projects={})))
    work_deps = SimpleNamespace(eligible=lambda *_args: [])

    assert routes._entitled_candidates(runtime, work_deps, "claude-01", "node-a", "worker-1", {}) == ([], "")


def test_related_hits_reads_memory_at_call_time(monkeypatch):
    expected = [{"key": "SHOP-3"}]
    calls = []
    monkeypatch.setattr(routes.memory, "related", lambda *args, **kwargs: calls.append((args, kwargs)) or expected)
    runtime = SimpleNamespace(ctx=SimpleNamespace(db="db"), logger=SimpleNamespace(warning=lambda *_args: None))
    ticket = {"project": "SHOP", "summary": "Need help", "key": "SHOP-4"}

    assert routes._related_hits(runtime, ticket) == expected
    assert calls == [(("db", "SHOP", "Need help"), {"exclude_ticket": "SHOP-4", "limit": 3})]


def test_ticket_epic_uses_the_supplied_projects_route():
    projects_routes = SimpleNamespace(ticket_epic=lambda ticket: {"epic": ticket["key"]})
    assert routes._ticket_epic(projects_routes, {"key": "SHOP-5"}) == {"epic": "SHOP-5"}


def test_claim_helper_reads_claim_work_from_its_module_at_call_time(monkeypatch):
    expected = {"claimed": False}
    calls = []
    monkeypatch.setattr(routes, "claim_work", lambda *args: calls.append(args) or expected)
    args = (object(), object(), object(), "node-a")

    assert routes._call_claim_work(*args) == expected
    assert calls == [args]
