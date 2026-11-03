"""JAIF-537: Adapter.run keeps its behaviour after being split into steps.

Each case runs a real child process (a short Python script) through an adapter whose parse_event only appends text
and records the golden result written from the unchanged function (tests/v4/fixtures/adapter_run_parity.json)."""
import json
import sys
import threading
from pathlib import Path

import pytest

from aiflow.v4.adapters import base
from aiflow.v4.adapters.base import Adapter, RunSpec

FIXTURE = Path(__file__).with_name("fixtures") / "adapter_run_parity.json"


class ScriptAdapter(Adapter):
    provider = "script"

    def __init__(self, script: str) -> None:
        super().__init__()
        self.script = script
        self.events: list[dict] = []

    def argv(self, spec):
        return [sys.executable, "-u", "-c", self.script]

    def parse_event(self, event, res):
        self.events.append(event)
        if "text" in event:
            res.final_text += event["text"] + "\n"


def emit(*lines: str, code: int = 0, sleep: float = 0) -> str:
    body = "import sys,time\n" + "".join(f"print({line!r}, flush=True)\n" for line in lines)
    if sleep:
        body += f"time.sleep({sleep})\n"
    return body + f"sys.exit({code})\n"


J = json.dumps
CASES = {
    "events_and_verdict": dict(script=emit(J({"text": "working"}), J({"text": "RESULT: DONE: all good"}))),
    "last_verdict_wins": dict(script=emit(J({"text": "VERDICT: FAIL first"}), J({"text": "RESULT: PASS: second"}))),
    "plain_lines": dict(script=emit("hello", "world")),
    "json_message_error": dict(script=emit(J({"message": "boom"}), code=1)),
    "plain_line_error": dict(script=emit("something broke", code=2)),
    "nested_error_message": dict(script=emit(J({"error": {"message": "deep failure"}}), code=3)),
    "quota": dict(script=emit("usage limit reached, resets at 5pm", code=1)),
    "auth": dict(script=emit("please run /login", code=1)),
    "network": dict(script=emit("connection refused", code=1)),
    "provider_without_text": dict(script=emit("plain", code=4)),
    "text_with_nonzero_exit": dict(script=emit(J({"text": "partial"}), code=5)),
    "quota_but_text_and_zero_exit": dict(script=emit(J({"text": "ok"}), "usage limit reached", code=0)),
    "long_output_keeps_tail": dict(script="import sys\nfor i in range(300): print('line', i, flush=True)\nsys.exit(1)\n"),
    "timeout": dict(script=emit(sleep=30), timeout_s=1),
    "idle": dict(script=emit(sleep=30), idle_s=1),
    "stop_before_start": dict(script=emit(sleep=30), stop=True),
    "progress_callback_raises": dict(script=emit(J({"text": "a"}), J({"text": "b"})), raising_progress=True),
    "on_start_called": dict(script=emit(J({"text": "x"})), with_on_start=True),
    "log_write": dict(script=emit(J({"text": "logged"}), "raw"), log="write"),
    "log_append": dict(script=emit("second"), log="append"),
    "container_hook_raises": dict(script=emit(sleep=30), hook_raises=True),
    "container_hook_ok": dict(script=emit(J({"text": "hooked"})), hook_ok=True),
}


def observe(name: str, tmp_path: Path) -> dict:
    case = CASES[name]
    adapter = ScriptAdapter(case["script"])
    log = tmp_path / "run.log"
    if case.get("log") == "append":
        log.write_text("first\n", encoding="utf-8")
    spec = RunSpec(prompt="p", cwd=str(tmp_path), account_home=str(tmp_path / "home"),
                   timeout_s=case.get("timeout_s", 20), idle_s=case.get("idle_s", 20),
                   log_path=str(log) if case.get("log") else "", log_append=case.get("log") == "append")
    seen: list = []
    stop = threading.Event()
    if case.get("stop"):
        stop.set()
    if case.get("hook_raises"):
        def hook(pid):
            seen.append("hook")
            raise RuntimeError("hook failed")
        spec.container_start_hook = hook
    elif case.get("hook_ok"):
        spec.container_start_hook = lambda pid: seen.append("hook")

    def progress(event):
        seen.append("progress")
        if case.get("raising_progress"):
            raise ValueError("ignored")

    raised = ""
    res = None
    try:
        res = adapter.run(spec, on_progress=progress, stop=stop,
                          on_start=(lambda: seen.append("start")) if case.get("with_on_start") else None)
    except RuntimeError as exc:
        raised = str(exc)
    out = {"raised": raised, "seen": seen, "events": adapter.events}
    if res is not None:
        out.update({"exit_code": res.exit_code, "final_text": res.final_text, "verdict": res.verdict,
                    "verdict_reason": res.verdict_reason, "error_kind": res.error_kind,
                    "error_message": res.error_message, "log_path": bool(res.log_path),
                    "duration_ok": 0 <= res.duration_s < 15})
    if case.get("log"):
        out["log"] = log.read_text(encoding="utf-8")
    return out


EXPECTED = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}


@pytest.mark.parametrize("name", list(CASES))
def test_run_matches_recorded_result(name, tmp_path):
    assert observe(name, tmp_path) == EXPECTED[name]


def test_every_case_is_recorded():
    assert set(EXPECTED) == set(CASES)
    assert len(CASES) >= 20
