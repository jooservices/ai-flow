import json
import sys
import textwrap
from pathlib import Path

import pytest

from aiflow.v4.adapters.base import RunResult, RunSpec
from aiflow.v4.adapters.claude import ClaudeAdapter
from aiflow.v4.adapters.codex import CodexAdapter
from aiflow.v4.adapters.grok import GrokAdapter


def test_claude_events_from_spike_s1():
    a, res = ClaudeAdapter(), RunResult()
    a.parse_event({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1792561800,
                   "rateLimitType": "five_hour", "unifiedWindows": {"five_hour": {"utilization": 0.16, "resetsAt": 1792561800},
                                                                     "seven_day": {"utilization": 0.64, "resetsAt": 1792638000}}}}, res)
    a.parse_event({"type": "result", "subtype": "success", "result": "done\nRESULT: DONE", "total_cost_usd": 0.0679,
                   "permission_denials": [{"tool_name": "Bash"}],
                   "usage": {"input_tokens": 8, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 370}}, res)
    assert res.quota["windows"]["seven_day"]["utilization"] == 0.64 and res.error_kind == ""
    assert res.tokens_in == 1008 and res.tokens_out == 370 and res.permission_denials == 1 and res.cost_usd == 0.0679
    rejected = RunResult()
    a.parse_event({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1792561800}}, rejected)
    assert rejected.error_kind == "quota" and rejected.reset_at.year >= 2026


def test_claude_records_only_failed_bash_tool_results_as_executed_commands():
    adapter, result = ClaudeAdapter(), RunResult()
    adapter.parse_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "php -v"}},
        {"type": "tool_use", "id": "tool-2", "name": "Read", "input": {"file_path": "README.md"}},
    ]}}, result)
    adapter.parse_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "bash: php: command not found", "is_error": True},
        {"type": "tool_result", "tool_use_id": "tool-2", "content": "bash: php: command not found", "is_error": True},
    ]}}, result)
    assert result.command_results == [{"command": "php -v", "output": "bash: php: command not found", "failed": True}]


def test_claude_preserves_shell_exit_code_as_command_evidence():
    adapter, result = ClaudeAdapter(), RunResult()
    adapter.parse_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "php -v"}},
    ]}}, result)
    adapter.parse_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "bash: php: command not found",
         "is_error": True, "exit_code": 127},
    ]}}, result)
    assert result.command_results == [{"command": "php -v", "output": "bash: php: command not found",
                                       "failed": True, "exit_code": 127}]


def test_codex_argv_disables_chatgpt_apps_and_keeps_options(tmp_path):
    a = CodexAdapter()
    cmd = a.argv(RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path), model="gpt-6-luna", effort="high"))
    assert cmd[cmd.index("features.apps=false") - 1] == "-c"
    assert 'approval_policy="never"' in cmd
    assert "sandbox_workspace_write.network_access=true" in cmd
    assert cmd[:5] == ["codex", "exec", "--json", "--cd", str(tmp_path)]
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("-m") + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="high"' in cmd
    assert cmd[-1] == "p"


def test_codex_and_grok_events_from_spikes():
    res = RunResult()
    CodexAdapter().parse_event({"type": "item.completed", "item": {"type": "agent_message", "text": "SUMMARY: x\nRESULT: DONE"}}, res)
    CodexAdapter().parse_event({"type": "turn.completed", "usage": {"input_tokens": 14152, "output_tokens": 17, "reasoning_output_tokens": 3}}, res)
    assert res.tokens_in == 14152 and res.tokens_out == 20 and "RESULT: DONE" in res.final_text
    g = RunResult()
    GrokAdapter().parse_event({"type": "error", "message": "Internal error: { \"message\": \"API error (status 402 Payment Required):"
                                                            " Grok Build usage balance exhausted\" }"}, g)
    assert g.error_kind == "quota"
    auth = RunResult()
    GrokAdapter().parse_event({"type": "error", "message": "You are not signed in. Please run /login."}, auth)
    assert auth.error_kind == "auth"


def test_codex_records_failed_command_execution_output():
    result = RunResult()
    CodexAdapter().parse_event({"type": "item.completed", "item": {
        "type": "command_execution", "command": "php -v", "aggregated_output": "bash: php: command not found",
        "exit_code": 127}}, result)
    CodexAdapter().parse_event({"type": "item.completed", "item": {
        "type": "command_execution", "command": "echo php", "aggregated_output": "bash: php: command not found",
        "exit_code": 0}}, result)
    assert result.command_results == [{"command": "php -v", "output": "bash: php: command not found", "exit_code": 127}]


def test_codex_command_event_records_whether_catalogue_executable_resolves_on_ai_path(monkeypatch):
    from aiflow.v4.core import toolchains

    monkeypatch.setattr(toolchains.shutil, "which",
                        lambda name, path=None: "/usr/bin/php" if name == "php" and path == "/has-php" else None)
    missing, installed = CodexAdapter(), CodexAdapter()
    missing._command_path = "/no-php"
    installed._command_path = "/has-php"
    event = {"type": "item.completed", "item": {
        "type": "command_execution", "command": "php -v", "aggregated_output": "bash: php: command not found",
        "exit_code": 127}}
    missing_result, installed_result = RunResult(), RunResult()

    missing.parse_event(event, missing_result)
    installed.parse_event(event, installed_result)

    assert missing_result.command_results[0]["executable_found_on_path"] is False
    assert installed_result.command_results[0]["executable_found_on_path"] is True


def test_codex_and_grok_keep_reported_quota_windows_when_cli_exposes_them():
    codex = RunResult()
    CodexAdapter().parse_event({"type": "token_count", "info": {"rate_limits": {
        "primary": {"used_percent": 64, "resets_at": "2026-10-22T16:00:00Z", "window_duration_mins": 300},
        "secondary": {"used_percent": 20, "resets_at": "2026-10-29T12:00:00Z", "window_duration_mins": 10080},
    }}}, codex)
    assert codex.quota["windows"]["five_hour"]["utilization"] == .64
    assert codex.quota["windows"]["seven_day"]["window_duration_minutes"] == 10080
    grok = RunResult()
    GrokAdapter().parse_event({"type": "usage", "quota": {"windows": {
        "weekly": {"used_percent": 0.3, "resets_at": "2026-10-29T12:00:00Z"}}}}, grok)
    assert grok.quota["windows"]["seven_day"]["utilization"] == 0.3


@pytest.mark.parametrize(("field", "normalized_field"), [("limit", "limit_tokens"), ("total", "total_tokens")])
def test_quota_windows_preserve_capacity_reported_with_used_tokens(field, normalized_field):
    result = RunResult()
    CodexAdapter().parse_event({"type": "token_count", "info": {"rate_limits": {
        "primary": {"used_tokens": 400, field: 1000}}}}, result)

    window = result.quota["windows"]["five_hour"]
    assert window["utilization"] == 0.4
    assert window[normalized_field] == 1000


def test_grok_runs_recorded_streaming_json_and_reads_answer_usage_and_cost(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "grok-streaming-json.jsonl"
    script = tmp_path / "recorded_grok.py"
    script.write_text(f"from pathlib import Path\nprint(Path({str(fixture)!r}).read_text(), end='')\n")
    adapter = GrokAdapter()
    adapter.argv = lambda _spec: [sys.executable, str(script)]

    result = adapter.run(RunSpec(prompt="Reply OK", cwd=str(tmp_path), account_home=str(tmp_path)))

    assert result.exit_code == 0 and result.error_kind == ""
    assert result.final_text == "OK"
    assert result.tokens_in == 10 and result.tokens_out == 1 and result.cost_usd == 0.00123


def test_quota_parser_requires_labeled_read_only_window_percentages():
    from aiflow.v4.worker.quota_cli import COMMANDS, parse_quota_text

    parsed = parse_quota_text("codex", "Rate limits: 5h 42% used · weekly 81% remaining")
    assert parsed == {"provider": "codex", "windows": {
        "five_hour": {"utilization": .42}, "weekly": {"utilization": .19}}}
    assert parse_quota_text("codex", "Context window: 42% used") == {}
    assert COMMANDS == {"codex": "/status", "claude": "/usage"}


def fake_cli(tmp_path: Path, lines: list[dict], exit_code: int = 0, sleep: float = 0) -> list[str]:
    script = tmp_path / "fake_cli.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys, time
        time.sleep({sleep})
        for line in {json.dumps(lines)!r} and json.loads({json.dumps(json.dumps(lines))}):
            print(json.dumps(line), flush=True)
        sys.exit({exit_code})
    """))
    return [sys.executable, str(script)]


def test_run_loop_reads_events_and_verdict(tmp_path):
    a = CodexAdapter()
    argv = fake_cli(tmp_path, [{"type": "item.completed", "item": {"type": "agent_message",
                                "text": "SUMMARY: added the block\nTESTS: phpunit ok\nRESULT: DONE"}},
                               {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}])
    a.argv = lambda spec: argv
    spec = RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path), log_path=str(tmp_path / "run.jsonl"))
    res = a.run(spec)
    assert res.exit_code == 0 and res.verdict == "DONE" and res.tokens_in == 10 and res.error_kind == ""
    assert (tmp_path / "run.jsonl").read_text().count("\n") == 2


def test_run_loop_can_append_continuation_events_and_signal_provider_start(tmp_path):
    a = CodexAdapter()
    log_path = tmp_path / "run.jsonl"
    a.argv = lambda spec: fake_cli(tmp_path, [{"type": "turn.completed"}])
    started = []
    first = RunSpec(prompt="first", cwd=str(tmp_path), account_home=str(tmp_path), log_path=str(log_path))
    a.run(first)
    continuation = RunSpec(prompt="follow up", cwd=str(tmp_path), account_home=str(tmp_path),
                           log_path=str(log_path), log_append=True)
    result = a.run(continuation, on_start=lambda: started.append(True))
    assert result.exit_code == 0 and started == [True]
    assert log_path.read_text().count("\n") == 2


def test_run_loop_captures_cli_credential_expiry_and_network_errors(tmp_path):
    a = CodexAdapter()
    expiry = "2026-10-25T10:00:00Z"
    a.argv = lambda spec: fake_cli(tmp_path, [{"type": "auth.status", "auth": {"expires_at": expiry}}])
    res = a.run(RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path)))
    assert res.credential_expires_at.isoformat() == "2026-10-25T10:00:00+00:00"
    assert res.to_dict()["credential_expires_at"] == "2026-10-25T10:00:00+00:00"

    script = tmp_path / "network.py"
    script.write_text("import sys\nprint('Could not resolve host: provider.invalid')\nsys.exit(1)\n")
    a.argv = lambda spec: [sys.executable, str(script)]
    failed = a.run(RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path)))
    assert failed.error_kind == "network"


def test_run_loop_quota_from_text_and_timeout(tmp_path):
    a = CodexAdapter()
    script = tmp_path / "q.py"
    script.write_text("import sys\nprint('ERROR: You have hit your usage limit. Try again at 9:15 PM.')\nsys.exit(1)\n")
    a.argv = lambda spec: [sys.executable, str(script)]
    res = a.run(RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path)))
    assert res.error_kind == "quota"
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n")
    a.argv = lambda spec: [sys.executable, str(slow)]
    res = a.run(RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path), timeout_s=1))
    assert res.error_kind == "timeout"


def test_claude_settings_carry_the_hook_and_default_to_relay(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFLOW4_POLICY_RELAY", raising=False)
    a = ClaudeAdapter()
    spec = RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path), control_url="http://10.44.0.10", run_token="t")
    a.prepare(spec)
    s = json.loads(a.settings_path(spec).read_text())
    hook = s["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["type"] == "http" and hook["url"] == "http://127.0.0.1:8471/v1/policy/decide"
    assert all(tool in s["hooks"]["PreToolUse"][0]["matcher"] for tool in
               ("Read", "Glob", "Grep", "WebFetch", "WebSearch"))
    assert hook["timeout"] == 65 and hook["onFailure"] == "block"
    assert "--permission-mode" in a.argv(spec) and a.env(spec)["AIFLOW4_RUN_TOKEN"] == "t"


def test_claude_settings_respect_policy_relay_toggle(tmp_path, monkeypatch):
    a = ClaudeAdapter()
    spec = RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path), control_url="http://10.44.0.10/")

    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "1")
    a.prepare(spec)
    hook = json.loads(a.settings_path(spec).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["url"] == "http://127.0.0.1:8471/v1/policy/decide"

    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")
    a.prepare(spec)
    hook = json.loads(a.settings_path(spec).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["url"] == "http://10.44.0.10/v1/policy/decide"


def test_ai_runs_keep_temp_files_and_caches_on_disk(tmp_path):
    from aiflow.v4.adapters.base import scratch_env
    home = tmp_path / "srv" / "accounts" / "codex-01"
    home.mkdir(parents=True)
    spec = RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(home))
    env = scratch_env(spec)
    assert env["TMPDIR"] == str(tmp_path / "srv" / "cache" / "ai" / "tmp") and Path(env["UV_CACHE_DIR"]).is_dir()
    argv = CodexAdapter().argv(spec)
    assert any("writable_roots" in a and str(tmp_path / "srv" / "cache" / "ai") in a for a in argv)


def test_scratch_root_belongs_to_the_ai_user_when_the_worker_is_root(tmp_path, monkeypatch):
    """Codex's sandbox creates <writable root>/.git; it fails when root owns the scratch root."""
    from aiflow.v4.adapters import base
    home = tmp_path / "srv" / "accounts" / "codex-01"
    home.mkdir(parents=True)
    owned = []
    monkeypatch.setattr(base.os, "geteuid", lambda: 0)
    monkeypatch.setattr(base.shutil, "chown", lambda p, u, g: owned.append((Path(p), u, g)))
    base.scratch_env(RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(home)))
    assert (tmp_path / "srv" / "cache" / "ai", "aiflow-worker", "aiflow-worker") in owned
