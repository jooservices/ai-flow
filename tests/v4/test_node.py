from types import SimpleNamespace

from aiflow.v4.node import agent


def test_node_tool_versions_include_codex_code_mode_host(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout=f"{cmd[0]} 1.0\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["codex-code-mode-host"] == "codex-code-mode-host 1.0"
    assert ["codex-code-mode-host", "--version"] in calls


def test_node_tool_versions_report_codex_code_mode_host_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "codex-code-mode-host":
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(stdout="ok\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert agent.tool_versions()["codex-code-mode-host"] == "missing"


def test_node_heartbeat_reports_policy_relay_health(monkeypatch):
    class FakeRelay:
        def __init__(self, control_url):
            assert control_url == "http://control.test"
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True
            return True

        def health(self):
            return {"enabled": True, "healthy": self.started and not self.stopped}

        def stop(self):
            self.stopped = True

    class FakeControlClient:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def call(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"desired": {"workers": [], "release": ""}}

    class StopAfterHeartbeat:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    relays = []
    clients = []

    def fake_relay(control_url):
        relay = FakeRelay(control_url)
        relays.append(relay)
        return relay

    def fake_client(*args, **kwargs):
        client = FakeControlClient(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "1")
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")
    monkeypatch.setattr(agent, "PolicyRelay", fake_relay)
    monkeypatch.setattr(agent, "ControlClient", fake_client)
    monkeypatch.setattr(agent, "tool_versions", lambda: {})
    monkeypatch.setattr(agent, "metrics", lambda _previous: ({"cpu_pct": 0}, (1, 1)))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent, "installed_release", lambda: "")

    agent.run_agent("http://control.test", "node1", "token", manage_workers=False, stop=StopAfterHeartbeat())

    heartbeat = clients[0].calls[0][0][2]
    assert heartbeat["metrics"]["policy_relay"] == {"enabled": True, "healthy": True}
    assert relays[0].started and relays[0].stopped
