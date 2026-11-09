"""Test the registered node heartbeat provider API."""

from types import SimpleNamespace

import pytest

from aiflow.v4.node.heartbeat_providers import (
    HEARTBEAT_PROVIDERS,
    HeartbeatContext,
    HeartbeatSources,
    collect_heartbeat_fields,
    register_heartbeat_provider,
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

    def record(name, result):
        events.append(name)
        return result

    class Relay:
        def health(self):
            return record("relay", {"enabled": True, "healthy": True})

    sources = HeartbeatSources(
        metrics=lambda previous: record("metrics", (metrics, (6, 7))) if previous == (4, 5) else None,
        tool_versions=lambda: record("tool_versions", {"new": "new 1"}),
        worker_states=lambda: record("workers", workers),
        installed_release=lambda: record("installed_release", "v4.2.0"),
        credential_expiries=lambda observed: record("credential_expiries", {"count": len(observed)}),
        system_conditions=lambda observed, fixed: record("system_conditions", ["healthy", *sorted(fixed)]),
        credential_checks=lambda observed: record("credential_checks", {"count": len(observed)}),
        services_snapshot=lambda services, lock: record("services", services),
        runtime_settings_inventory=lambda: record("runtime_settings", {"DEMO": ["API_KEY"]}),
        read_env_file=lambda path: record(f"env:{path}", {"API_KEY": "set", "EMPTY": ""}),
        hostname=lambda: record("hostname", "node-test"),
        now=lambda: record("now", 1000),
        version="fallback",
    )
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


def test_heartbeat_provider_validation_error_propagates_from_collection():
    context = _context([])
    context.slots = True

    with pytest.raises(TypeError, match="heartbeat field slots_total must be int"):
        collect_heartbeat_fields(context)


def test_heartbeat_provider_registration_preserves_explicit_orders():
    registry = {}
    events = []
    specs = (
        ("environment_items", 3, 3),
        ("workers", 0, 2),
        ("tools", 1, 1),
        ("metrics", 2, 0),
    )
    for name, order, prepare_order in specs:
        register_heartbeat_provider(
            name,
            str,
            lambda _context, field=name: events.append(f"collect:{field}") or field,
            order=order,
            prepare=lambda _context, field=name: events.append(f"prepare:{field}"),
            prepare_order=prepare_order,
            registry=registry,
        )
    reverse_registration = dict(reversed(tuple(registry.items())))

    payload = collect_heartbeat_fields(SimpleNamespace(), reverse_registration)

    assert tuple(payload) == ("workers", "tools", "metrics", "environment_items")
    assert events == [
        "prepare:metrics",
        "prepare:tools",
        "prepare:workers",
        "prepare:environment_items",
        "collect:workers",
        "collect:tools",
        "collect:metrics",
        "collect:environment_items",
    ]


def test_heartbeat_registry_is_read_only_and_has_explicit_payload_orders():
    assert tuple(HEARTBEAT_PROVIDERS) == FIELD_ORDER
    assert tuple(provider.order for provider in HEARTBEAT_PROVIDERS.values()) == tuple(range(16))
    with pytest.raises(TypeError):
        HEARTBEAT_PROVIDERS["extra"] = HEARTBEAT_PROVIDERS["tools"]


def test_heartbeat_registration_rejects_duplicate_names_and_payload_orders():
    registry = {}
    register_heartbeat_provider("first", str, lambda _context: "first", order=1, registry=registry)

    with pytest.raises(ValueError, match="duplicate heartbeat provider name first"):
        register_heartbeat_provider("first", str, lambda _context: "again", order=2, registry=registry)
    with pytest.raises(ValueError, match="duplicate heartbeat provider order 1"):
        register_heartbeat_provider("second", str, lambda _context: "second", order=1, registry=registry)


def test_heartbeat_registration_rejects_duplicate_preparation_order():
    registry = {}
    prepare = lambda _context: None
    register_heartbeat_provider(
        "first", str, lambda _context: "first", order=1, prepare=prepare, prepare_order=0, registry=registry
    )

    with pytest.raises(ValueError, match="duplicate heartbeat preparation order 0"):
        register_heartbeat_provider(
            "second", str, lambda _context: "second", order=2, prepare=prepare, prepare_order=0, registry=registry
        )


def test_heartbeat_registration_requires_preparation_and_order_together():
    with pytest.raises(ValueError, match="preparation and its order"):
        register_heartbeat_provider(
            "first", str, lambda _context: "first", order=1, prepare=lambda _context: None, registry={}
        )
