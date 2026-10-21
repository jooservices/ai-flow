import threading
from types import SimpleNamespace

from aiflow.v4.node import agent


def test_node_tool_versions_include_codex_code_mode_host(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "sh" and "codex-code-mode-host" in cmd[-1]:  # looked up next to the codex binary
            return SimpleNamespace(returncode=0, stdout="present /opt/aiflow/tools/codex/codex-code-mode-host\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} 1.0\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["codex-code-mode-host"].startswith("present ")
    assert versions["wp"] == "wp 1.0" and ["wp", "--version", "--allow-root"] in calls


def test_node_tool_versions_report_codex_code_mode_host_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "sh" and "codex-code-mode-host" in cmd[-1]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert agent.tool_versions()["codex-code-mode-host"] == "missing"


def test_node_tool_versions_report_nonzero_exit_with_output_as_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "php":
            return SimpleNamespace(returncode=1, stdout="", stderr="PHP startup error\n")
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["php"] == "missing"
    assert versions["composer"] == "composer version"


def test_node_tool_versions_report_success_without_output_as_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "composer":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["composer"] == "missing"
    assert versions["php"] == "php version"


def test_node_tool_versions_report_stack_tools(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "wp":
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["php"] == "php version"
    assert versions["composer"] == "composer version"
    assert versions["python3"] == "python3 version"
    assert versions["wp"] == "missing"


def test_heartbeat_carries_the_version_or_missing_for_each_project_tool(monkeypatch):
    heartbeats = []
    stop = threading.Event()

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, path, body=None, **kwargs):
            heartbeats.append(body)
            stop.set()
            return {"desired": {"accounts": [], "workers": [], "projects": ["TESTWP"],
                                "tools": ["php", "composer", "wp"]}}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "wp":
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent, "ControlClient", FakeApi)
    monkeypatch.setattr(agent, "metrics", lambda prev: ({"hostname": "node1"}, (1, 1)))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")

    agent.run_agent("http://control", "node1", "token", every_s=0, stop=stop, manage_workers=False)

    tools = heartbeats[0]["tools"]
    assert tools["php"] == "php version"
    assert tools["composer"] == "composer version"
    assert tools["wp"] == "missing"


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


def test_a_slow_tool_check_is_not_reported_missing(monkeypatch):
    import subprocess as sp

    def fake_run(cmd, **kwargs):
        if cmd[0] == "composer":
            raise sp.TimeoutExpired(cmd, 30)
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} 1.0\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    from aiflow.v4.core.toolchains import missing_tools
    versions = agent.tool_versions()
    assert versions["composer"] == "timeout" and missing_tools(versions, ["composer"]) == []
