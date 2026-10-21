import threading
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


def test_node_tool_versions_report_stack_tools(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "wp":
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(stdout=f"{cmd[0]} version\n", stderr="")

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
        return SimpleNamespace(stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent, "ControlClient", FakeApi)
    monkeypatch.setattr(agent, "metrics", lambda prev: ({"hostname": "node1"}, (1, 1)))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent.subprocess, "run", fake_run)

    agent.run_agent("http://control", "node1", "token", every_s=0, stop=stop, manage_workers=False)

    tools = heartbeats[0]["tools"]
    assert tools["php"] == "php version"
    assert tools["composer"] == "composer version"
    assert tools["wp"] == "missing"
