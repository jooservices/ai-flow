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
