"""Lock down the heartbeat payload and call order across the provider refactor."""

from types import SimpleNamespace

from aiflow.v4.node import agent


def test_heartbeat_collection_preserves_payload_and_side_effect_order(monkeypatch):
    events = []
    workers = [{"account": "codex-01", "state": "working", "alive": True}]
    node_metrics = {"cpu_pct": 23.5}
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

    monkeypatch.setattr(agent, "metrics", lambda previous: record("metrics", (node_metrics, (6, 7))))
    monkeypatch.setattr(agent.time, "time", lambda: record("now", 1000))
    monkeypatch.setattr(agent, "tool_versions", lambda: record("tool_versions", {"new": "new 1"}))
    monkeypatch.setattr(agent, "worker_states", lambda: record("workers", workers))
    monkeypatch.setattr(agent, "read_env_file", lambda path: record(f"env:{path}", {"API_KEY": "set", "EMPTY": ""}))
    monkeypatch.setattr(agent, "installed_release", lambda: record("installed_release", "v4.2.0"))
    monkeypatch.setattr(
        agent, "_services_snapshot", lambda services, lock: record("services", services)
    )
    monkeypatch.setattr(agent.socket, "gethostname", lambda: record("hostname", "node-test"))
    monkeypatch.setattr(agent, "credential_expiries", lambda observed: record("credential_expiries", {"count": len(observed)}))
    monkeypatch.setattr(
        agent,
        "_system_machine_conditions",
        lambda observed, fixed: record("system_conditions", ["healthy", *sorted(fixed)]),
    )
    monkeypatch.setattr(agent, "_credential_conditions", lambda observed: record("credential_checks", {"count": len(observed)}))
    monkeypatch.setattr(agent, "runtime_settings_inventory", lambda: record("runtime_settings", {"DEMO": ["API_KEY"]}))

    payload = agent.collect_heartbeat(state, "node-test", 4, Relay(), "public-key")

    assert tuple(payload) == (
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
    assert events == [
        "metrics",
        "now",
        "workers",
        "env:/etc/aiflow/projects/DEMO.env",
        "installed_release",
        "services",
        "hostname",
        "relay",
        "credential_expiries",
        "system_conditions",
        "credential_checks",
        "runtime_settings",
    ]
    assert payload == {
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
    assert payload["workers"] is workers
    assert state.previous_metrics == (6, 7)
