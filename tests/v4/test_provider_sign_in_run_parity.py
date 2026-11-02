"""Characterization of provider sign-in orchestration before it is split into steps."""
from __future__ import annotations

import json
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from aiflow.v4.node import provider_sign_in


FIXTURE = Path(__file__).parent / "fixtures" / "provider_sign_in_run_parity.jsonl"
MASTER_FD = 100
SLAVE_FD = 101


class _Process:
    def __init__(self, scenario: dict, events: list):
        self.scenario = scenario
        self.events = events
        self.pid = 4242
        self._timed_out = False
        self.cancel = None

    def poll(self):
        result = self.scenario.get("poll", 0)
        self.events.append(["poll", result])
        return result

    def wait(self, timeout=None):
        self.events.append(["wait", timeout])
        if timeout == 5 and self.scenario.get("term_timeout") and not self._timed_out:
            self._timed_out = True
            raise subprocess.TimeoutExpired("provider", timeout)
        if timeout == 10 and self.scenario.get("cancel_after_wait") and self.cancel:
            self.cancel.set()
        return self.scenario.get("return_code", 0)


class _APIRecorder:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None, **kwargs):
        self.calls.append([method, path, body, kwargs.get("retries")])


def _load_cases() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


class _RuntimeFakes:
    def __init__(self, case: dict, events: list):
        self.case = case
        self.scenario = case.get("scenario", {})
        self.events = events
        self.process = _Process(self.scenario, events)
        self.chunks = iter(self.scenario.get("chunks", []))
        self.select_steps = iter(self.scenario.get("select", []))
        self.api_calls = 0

    def call(self, method, path, body=None, **kwargs):
        self.api_calls += 1
        self.events.append(["api", method, path, body, kwargs.get("retries")])
        if self.scenario.get("api_fail_at") == self.api_calls:
            raise RuntimeError("API unavailable")

    def open_pty(self):
        self.events.append(["openpty"])
        return MASTER_FD, SLAVE_FD

    def popen(self, args, **kwargs):
        env = kwargs["env"]
        self.events.append(["popen", args, env["PATH"], env["HOME"], kwargs["stdin"], kwargs["stdout"],
                            kwargs["stderr"], kwargs["close_fds"], kwargs["start_new_session"]])
        if self.scenario.get("popen_error"):
            raise OSError(self.scenario["popen_error"])
        return self.process

    def select_ready(self, _readers, _writers, _errors, _timeout):
        step = next(self.select_steps, "quiet")
        self.events.append(["select", step])
        return ([MASTER_FD] if step == "ready" else [], [], [])

    def read_chunk(self, _fd, _size):
        chunk = next(self.chunks, "")
        if chunk == "EIO":
            self.events.append(["read-error", "OSError"])
            raise OSError("pty closed")
        self.events.append(["read", chunk])
        return chunk.encode("utf-8")

    def close_fd(self, fd):
        self.events.append(["close", fd])

    def kill_group(self, pid, sig):
        self.events.append(["killpg", pid, sig])

    def write_fd(self, fd, data):
        self.events.append(["write", fd, data.decode("ascii")])
        return len(data)

    def warn(self, message, *args):
        self.events.append(["log-warning", message, list(args)])

    def ensure_home(self, account, root):
        self.events.append(["ensure", account, root])
        return self.scenario.get("home_ok", True)

    def install(self, monkeypatch):
        monkeypatch.setenv("PATH", "/test/bin:/usr/bin")
        monkeypatch.setattr(provider_sign_in.pty, "openpty", self.open_pty)
        monkeypatch.setattr(provider_sign_in.subprocess, "Popen", self.popen)
        monkeypatch.setattr(provider_sign_in.select, "select", self.select_ready)
        monkeypatch.setattr(provider_sign_in.os, "read", self.read_chunk)
        monkeypatch.setattr(provider_sign_in.os, "close", self.close_fd)
        monkeypatch.setattr(provider_sign_in.os, "killpg", self.kill_group)
        monkeypatch.setattr(provider_sign_in.os, "write", self.write_fd)
        monkeypatch.setattr(provider_sign_in.log, "warning", self.warn)
        monkeypatch.setattr(provider_sign_in, "ensure_account_home", self.ensure_home)
        if "command" in self.scenario:
            provider = self.case["request"].get("provider")
            monkeypatch.setattr(provider_sign_in, "SIGN_IN_COMMANDS", {provider: self.scenario["command"]})


def _run_case(case: dict, monkeypatch) -> list:
    events: list = []
    runtime = _RuntimeFakes(case, events)
    runtime.install(monkeypatch)
    cancel = threading.Event()
    runtime.process.cancel = cancel
    if runtime.scenario.get("cancel"):
        cancel.set()
    provider_sign_in.run_provider_sign_in(runtime, "node-01", case["request"],
                                         cancel, "/accounts")
    return events


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["name"])
def test_run_provider_sign_in_matches_recorded_calls(case, monkeypatch):
    assert _run_case(case, monkeypatch) == case["expected"]


def test_valid_request_helper_covers_account_and_request_id_boundaries():
    valid = provider_sign_in._valid_sign_in_request
    assert valid("a" * 128, "req-01", False)
    assert not valid("a" * 129, "req-01", False)
    assert not valid("_node", "req-01", False)
    assert not valid("node-01", "", False)
    assert valid("node-01", "", True)


def test_unsupported_provider_helper_reports_only_private_accounts():
    api = _APIRecorder()
    provider_sign_in._report_unsupported_provider(api, "node-01", "node-01", "req-01", False)
    provider_sign_in._report_unsupported_provider(api, "node-01", "node-02", "req-02", True)
    assert api.calls == [["POST", "/v1/accounts/node-01/sign-in/status",
                          {"request_id": "req-01", "status": "failed", "sign_in_url": "",
                           "code": "", "error": "Provider sign-in is unsupported."}, 1]]


def test_prepare_environment_helper_preserves_setup_order_and_values(monkeypatch):
    calls = []
    api = _APIRecorder()
    monkeypatch.setenv("PATH", "/test/bin")
    monkeypatch.setattr(provider_sign_in, "ensure_account_home",
                        lambda account, root: calls.append(["ensure", account, root]) or True)
    env = provider_sign_in._prepare_sign_in_environment(api, "node-01", "node-01", "req-01", False, "/accounts")
    assert calls == [["ensure", "node-01", "/accounts"]]
    assert api.calls[0][2]["status"] == "starting"
    assert env == {"PATH": "/test/bin", "HOME": "/accounts/node-01", "USER": "aiflow-worker",
                   "LOGNAME": "aiflow-worker", "TERM": "xterm-256color",
                   "CODEX_HOME": "/accounts/node-01/.codex",
                   "CLAUDE_CONFIG_DIR": "/accounts/node-01/.claude",
                   "XDG_DATA_HOME": "/accounts/node-01/.local/share"}


def test_prepare_environment_helper_uses_default_path_when_unset(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setattr(provider_sign_in, "ensure_account_home", lambda *_args: True)
    env = provider_sign_in._prepare_sign_in_environment(
        _APIRecorder(), "node-01", "node-01", "req-01", True, "/accounts")
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


@pytest.mark.parametrize(("output", "expected"), [
    ("Press c to copy URL", True),
    ("Show authentication URL with c", True),
    ("Waiting for sign-in", False),
])
def test_copy_prompt_helper_matches_provider_terminal_prompts(output, expected):
    assert provider_sign_in._copy_auth_url_is_requested(output) is expected


def test_stop_process_helper_escalates_after_timeout(monkeypatch):
    events = []
    process = _Process({"term_timeout": True}, events)
    monkeypatch.setattr(provider_sign_in.os, "killpg", lambda pid, sig: events.append(["killpg", pid, sig]))
    provider_sign_in._stop_provider_sign_in(process)
    assert events == [["killpg", 4242, signal.SIGTERM], ["wait", 5],
                      ["killpg", 4242, signal.SIGKILL], ["wait", 5]]


def test_read_chunk_helper_decodes_and_treats_closed_pty_as_eof(monkeypatch):
    monkeypatch.setattr(provider_sign_in.os, "read", lambda _fd, _size: b"device output")
    assert provider_sign_in._read_provider_sign_in_chunk(MASTER_FD) == "device output"

    def closed_pty(_fd, _size):
        raise OSError("closed")

    monkeypatch.setattr(provider_sign_in.os, "read", closed_pty)
    assert provider_sign_in._read_provider_sign_in_chunk(MASTER_FD) == ""


def test_relay_output_helper_sends_private_handoff_and_updates_state(monkeypatch):
    api = _APIRecorder()
    writes = []
    monkeypatch.setattr(provider_sign_in.os, "write", lambda fd, data: writes.append([fd, data]) or len(data))
    result = provider_sign_in._relay_provider_sign_in_output(
        api, "node-01", "node-01", "req-01", False, MASTER_FD,
        "Press c to copy URL https://auth.example.test/device code ABCD-1234", "", ("", ""), False)
    assert result == ("Press c to copy URL https://auth.example.test/device code ABCD-1234",
                      ("https://auth.example.test/device", "ABCD-1234"), True)
    assert writes == [[MASTER_FD, b"c"]]
    assert api.calls[0][2]["status"] == "waiting"
    assert api.calls[0][2]["code"] == "ABCD-1234"


def test_monitor_helper_returns_exit_code_and_last_handoff(monkeypatch):
    events = []
    process = _Process({"chunks": ["https://auth.example.test/device code ABCD-1234"]}, events)
    chunks = iter(["https://auth.example.test/device code ABCD-1234"])
    selects = iter([[MASTER_FD], []])
    api = _APIRecorder()
    monkeypatch.setattr(provider_sign_in.select, "select", lambda *_args: (next(selects), [], []))
    monkeypatch.setattr(provider_sign_in.os, "read", lambda *_args: next(chunks).encode())
    result = provider_sign_in._monitor_provider_sign_in(
        api, "node-01", "node-01", "req-01", False, threading.Event(), process, MASTER_FD)
    assert result == (0, ("https://auth.example.test/device", "ABCD-1234"))
    assert api.calls[0][2]["status"] == "waiting"


def test_result_helper_reports_private_failure_and_shared_success():
    api = _APIRecorder()
    details = ("https://auth.example.test/device", "ABCD-1234")
    provider_sign_in._report_provider_sign_in_result(api, "node-01", "node-01", "req-01", False, 7, details)
    provider_sign_in._report_provider_sign_in_result(api, "node-01", "node-02", "req-02", True, 0, details)
    assert api.calls[0][2] == {"request_id": "req-01", "status": "failed",
                               "sign_in_url": details[0], "code": details[1],
                               "error": "Provider sign-in exited with status 7."}
    assert api.calls[1] == ["POST", "/v1/accounts/node-02/signin-complete", {"request_id": "req-02"}, 1]


def test_process_helper_closes_both_descriptors_and_reports_result(monkeypatch):
    events = []
    process = _Process({}, events)
    reported = []
    monkeypatch.setattr(provider_sign_in.pty, "openpty", lambda: (MASTER_FD, SLAVE_FD))
    monkeypatch.setattr(provider_sign_in.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(provider_sign_in.os, "close", lambda fd: events.append(["close", fd]))
    monkeypatch.setattr(provider_sign_in, "_monitor_provider_sign_in", lambda *_args: (0, ("", "")))
    monkeypatch.setattr(provider_sign_in, "_report_provider_sign_in_result",
                        lambda *args: reported.append(args))
    provider_sign_in._run_provider_sign_in_process(
        _APIRecorder(), "node-01", "node-01", "req-01", False, threading.Event(), ["codex", "login"], {})
    assert events == [["close", SLAVE_FD], ["close", MASTER_FD]]
    assert len(reported) == 1


def test_error_helper_reports_type_and_redacted_log(monkeypatch):
    api = _APIRecorder()
    logs = []
    monkeypatch.setattr(provider_sign_in.log, "warning", lambda *args: logs.append(args))
    provider_sign_in._handle_provider_sign_in_error(
        api, "node-01", "node-01", "req-01", False, threading.Event(), RuntimeError("private detail"))
    assert api.calls[0][2]["error"] == "Provider sign-in could not start (RuntimeError)."
    assert logs == [("provider sign-in failed for %s (%s)", "node-01", "RuntimeError")]


