"""Characterize heartbeat collection and test the registered provider API."""

from types import SimpleNamespace

import pytest

from aiflow.v4.node import agent
from aiflow.v4.node.heartbeat_providers import (
    HEARTBEAT_PROVIDERS,
    HeartbeatContext,
    HeartbeatSources,
    collect_heartbeat_fields,
)


FIELD_ORDER = (
    "slots_total",
    "slots_used",
    "release_version",
    "release_protocol",
    "tools",
    "services",
    "metrics",
    "workers",
    "credential_expiries",
    "system_conditions",
    "credential_checks",
    "auto_fixed_tools",
    "auto_fixed_conditions",
    "runtime_settings",
    "runtime_settings_public_key",
    "environment_items",
)


def _record(events, name, result):
    events.append(name)
    return result


def _context(events):
    workers = [{"account": "codex-01", "state": "working", "alive": True}]
    metrics = {"cpu_pct": 23.5}
    state = SimpleNamespace(
        tools={"git": "git 1"},
        tools_at=900,
        previous_metrics=(4, 5),
        project_environment={"DEMO": ["API_KEY", "EMPTY"]},
        service_state={"redis": {"healthy": True}},
        service_lock=object(),
        auto_fixed_tools={"tool-a"},
        auto_fixed_conditions={"disk-space"},
    )
    sources = HeartbeatSources(
        metrics=lambda previous: _record(events, "metrics", (metrics, (6, 7))),
        tool_versions=lambda: _record(events, "tool_versions", {"new": "new 1"}),
        worker_states=lambda: _record(events, "workers", workers),
        installed_release=lambda: _record(events, "installed_release", "v4.2.0"),
        credential_expiries=lambda observed: _record(events, "credential_expiries", {"count": len(observed)}),
        system_conditions=lambda observed, fixed: _record(events, "system_conditions", ["healthy", *sorted(fixed)]),
        credential_checks=lambda observed: _record(events, "credential_checks", {"count": len(observed)}),
        services_snapshot=lambda services, lock: _record(events, "services", services),
        runtime_settings_inventory=lambda: _record(events, "runtime_settings", {"DEMO": ["API_KEY"]}),
        read_env_file=lambda path: _record(events, f"env:{path}", {"API_KEY": "set", "EMPTY": ""}),
        hostname=lambda: _record(events, "hostname", "node-test"),
        now=lambda: _record(events, "now", 1000),
        version="fallback",
    )

    class Relay:
        def health(self):
            return _record(events, "relay", {"enabled": True, "healthy": True})

    return HeartbeatContext(state, 4, Relay(), "public-key", sources)


@pytest.mark.parametrize(
    ("now_values", "tools_at", "expected_events", "expected_tools", "expected_timestamp"),
    [
        ((601.0, 602.0), 0.0, ["now", "refresh", "now"], {"git": "git 1"}, 602.0),
        ((1000.0,), 500.0, ["now"], {"cached": "old"}, 500.0),
    ],
)
def test_tools_preparation_preserves_stale_and_fresh_refresh_timing(
    now_values, tools_at, expected_events, expected_tools, expected_timestamp
):
    events = []
    clock = iter(now_values)
    state = SimpleNamespace(tools={"cached": "old"}, tools_at=tools_at)
    context = SimpleNamespace(
        state=state,
        sources=SimpleNamespace(
            now=lambda: events.append("now") or next(clock),
            tool_versions=lambda: events.append("refresh") or {"git": "git 1"},
        ),
    )
    prepare = HEARTBEAT_PROVIDERS["tools"].prepare
    assert prepare is not None

    prepare(context)

    assert events == expected_events
    assert (state.tools, state.tools_at) == (expected_tools, expected_timestamp)


def test_heartbeat_integer_validation_rejects_bool_with_existing_error():
    with pytest.raises(TypeError, match="heartbeat field slots_total must be int"):
        HEARTBEAT_PROVIDERS["slots_total"].validate(True)


def test_heartbeat_collection_validation_error_propagates():
    events = []
    context = _context(events)
    context.slots = True

    with pytest.raises(TypeError, match="heartbeat field slots_total must be int"):
        collect_heartbeat_fields(context)

    assert events == ["metrics", "now", "workers", "env:/etc/aiflow/projects/DEMO.env"]


def _patch_agent_sources(monkeypatch, events, workers, node_metrics):
    record = lambda name, result: _record(events, name, result)
    monkeypatch.setattr(agent, "metrics", lambda previous: record("metrics", (node_metrics, (6, 7))))
    monkeypatch.setattr(agent.time, "time", lambda: record("now", 1000))
    monkeypatch.setattr(agent, "tool_versions", lambda: record("tool_versions", {"new": "new 1"}))
    monkeypatch.setattr(agent, "worker_states", lambda: record("workers", workers))
    monkeypatch.setattr(
        agent, "read_env_file", lambda path: record(f"env:{path}", {"API_KEY": "set", "EMPTY": ""})
    )
    monkeypatch.setattr(agent, "installed_release", lambda: record("installed_release", "v4.2.0"))
    monkeypatch.setattr(agent, "_services_snapshot", lambda services, lock: record("services", services))
    monkeypatch.setattr(agent.socket, "gethostname", lambda: record("hostname", "node-test"))
    monkeypatch.setattr(
        agent, "credential_expiries", lambda observed: record("credential_expiries", {"count": len(observed)})
    )
    monkeypatch.setattr(
        agent,
        "_system_machine_conditions",
        lambda observed, fixed: record("system_conditions", ["healthy", *sorted(fixed)]),
    )
    monkeypatch.setattr(
        agent, "_credential_conditions", lambda observed: record("credential_checks", {"count": len(observed)})
    )
    monkeypatch.setattr(
        agent, "runtime_settings_inventory", lambda: record("runtime_settings", {"DEMO": ["API_KEY"]})
    )


def _expected_payload(workers):
    return {
        "slots_total": 4,
        "slots_used": 1,
        "release_version": "v4.2.0",
        "release_protocol": 1,
        "tools": {"git": "git 1"},
        "services": {"redis": {"healthy": True}},
        "metrics": {"cpu_pct": 23.5, "hostname": "node-test", "policy_relay": {"enabled": True, "healthy": True}},
        "workers": workers,
        "credential_expiries": {"count": 1},
        "system_conditions": ["healthy", "disk-space"],
        "credential_checks": {"count": 1},
        "auto_fixed_tools": ["tool-a"],
        "auto_fixed_conditions": ["disk-space"],
        "runtime_settings": {"DEMO": ["API_KEY"]},
        "runtime_settings_public_key": "public-key",
        "environment_items": {"DEMO": ["API_KEY"]},
    }


def test_heartbeat_collection_preserves_payload_and_side_effect_order(monkeypatch):
    events = []
    workers = [{"account": "codex-01", "state": "working", "alive": True}]
    node_metrics = {"cpu_pct": 23.5}
    state = SimpleNamespace(
        tools={"git": "git 1"}, tools_at=900, previous_metrics=(4, 5),
        project_environment={"DEMO": ["API_KEY", "EMPTY"]},
        service_state={"redis": {"healthy": True}}, service_lock=object(),
        auto_fixed_tools={"tool-a"}, auto_fixed_conditions={"disk-space"},
    )

    class Relay:
        def health(self):
            return _record(events, "relay", {"enabled": True, "healthy": True})

    _patch_agent_sources(monkeypatch, events, workers, node_metrics)
    payload = agent.collect_heartbeat(state, "node-test", 4, Relay(), "public-key")

    assert tuple(payload) == FIELD_ORDER
    assert events == [
        "metrics", "now", "workers", "env:/etc/aiflow/projects/DEMO.env", "installed_release",
        "services", "relay", "hostname", "credential_expiries", "system_conditions",
        "credential_checks", "runtime_settings",
    ]
    assert payload == _expected_payload(workers)
    assert payload["workers"] is workers
    assert state.previous_metrics == (6, 7)


def _registration_api():
    from aiflow.v4.node import heartbeat_providers

    register = getattr(heartbeat_providers, "register_heartbeat_provider", None)
    if register is None:
        pytest.skip("provider registration API is not present in the baseline revision")
    return heartbeat_providers


def test_heartbeat_provider_registration_preserves_explicit_orders():
    api = _registration_api()
    registry = {}
    events = []
    specs = (
        ("environment_items", 3, 3),
        ("workers", 0, 2),
        ("tools", 1, 1),
        ("metrics", 2, 0),
    )
    for name, order, prepare_order in specs:
        api.register_heartbeat_provider(
            name, str, lambda _context, field=name: events.append(f"collect:{field}") or field,
            order=order,
            prepare=lambda _context, field=name: events.append(f"prepare:{field}"),
            prepare_order=prepare_order, registry=registry,
        )
    reverse_registration = dict(reversed(tuple(registry.items())))

    payload = api.collect_heartbeat_fields(SimpleNamespace(), reverse_registration)

    assert tuple(payload) == ("workers", "tools", "metrics", "environment_items")
    assert events == [
        "prepare:metrics", "prepare:tools", "prepare:workers", "prepare:environment_items",
        "collect:workers", "collect:tools", "collect:metrics", "collect:environment_items",
    ]


def test_heartbeat_registry_is_read_only_and_has_explicit_payload_orders():
    api = _registration_api()
    assert tuple(api.HEARTBEAT_PROVIDERS) == FIELD_ORDER
    assert tuple(provider.order for provider in api.HEARTBEAT_PROVIDERS.values()) == tuple(range(16))
    with pytest.raises(TypeError):
        api.HEARTBEAT_PROVIDERS["extra"] = api.HEARTBEAT_PROVIDERS["tools"]


def test_heartbeat_registration_rejects_duplicate_names_and_payload_orders():
    api = _registration_api()
    registry = {}
    api.register_heartbeat_provider("first", str, lambda _context: "first", order=1, registry=registry)

    with pytest.raises(ValueError, match="duplicate heartbeat provider name first"):
        api.register_heartbeat_provider("first", str, lambda _context: "again", order=2, registry=registry)
    with pytest.raises(ValueError, match="duplicate heartbeat provider order 1"):
        api.register_heartbeat_provider("second", str, lambda _context: "second", order=1, registry=registry)


def test_heartbeat_registration_rejects_duplicate_preparation_order():
    api = _registration_api()
    registry = {}
    prepare = lambda _context: None
    api.register_heartbeat_provider(
        "first", str, lambda _context: "first", order=1, prepare=prepare,
        prepare_order=0, registry=registry,
    )

    with pytest.raises(ValueError, match="duplicate heartbeat preparation order 0"):
        api.register_heartbeat_provider(
            "second", str, lambda _context: "second", order=2, prepare=prepare,
            prepare_order=0, registry=registry,
        )


@pytest.mark.parametrize(
    ("prepare", "prepare_order"),
    [(lambda _context: None, None), (None, 0)],
)
def test_heartbeat_registration_requires_preparation_and_order_together(prepare, prepare_order):
    api = _registration_api()
    with pytest.raises(ValueError, match="preparation and its order"):
        api.register_heartbeat_provider(
            "first", str, lambda _context: "first", order=1, prepare=prepare,
            prepare_order=prepare_order, registry={},
        )
