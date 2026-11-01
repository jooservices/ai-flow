"""Characterization of run_master: literal traces recorded from the unchanged function.

The expected traces live in tests/v4/fixtures/master_run_parity.jsonl. Regenerate that file only by
running the throw-away recorder against the unchanged function; the assertions stay fixed across the split.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.worker import master as worker_master
from aiflow.v4.worker.client import Conflict
from aiflow.v4.worker.dev import Claimed, Heartbeat

FIXTURE = Path(__file__).parent / "fixtures" / "master_run_parity.jsonl"
RESET_AT = datetime(2026, 4, 2, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)
RUN_AS = ["runuser", "-u", "aiflow-worker", "--"]
REMINDER = "STRICT FORMAT RETRY"


class ScriptError(BaseException):
    """A missing scripted response. BaseException so run_master does not turn it into a worker error."""


def snap(**overrides):
    data = {"open_incidents": [], "needs_owner": [], "running_clocks": [], "offline_nodes": [],
            "quota_accounts": [], "quota_incidents": []}
    data.update(overrides)
    return data


def plan(summary="checked", actions=None) -> str:
    return json.dumps({"summary": summary, "actions": [] if actions is None else actions})


def ok(text=None, **extra):
    item = {"final_text": plan() if text is None else text, "error_kind": "", "error_message": "",
            "tokens_in": 2, "tokens_out": 3, "cost_usd": 1, "quota": {}}
    item.update(extra)
    return item


def err(kind, message="boom", **extra):
    return ok(text="", error_kind=kind, error_message=message, **extra)


def incident_snapshot():
    return snap(open_incidents=[{"kind": "run_slow"}])


def allowed_action():
    return {"action": "retry", "ticket": "T-1", "intent": "put it back", "category": "flow", "headline": "h",
            "what": "w", "why": "y", "result": "r", "next": "n", "fields": {"a": 1}, "add_labels": ["a"],
            "remove_labels": ["b"], "kind": "k", "subject": "s", "severity": "warning", "title": "t", "detail": "d",
            "target_status": "To Do", "options": ["Wait", "Stop"], "observation_key": "ticket:T-1:1",
            "account_id": "acc", "pause_minutes": 15, "target_run_id": "run-x", "signature": "sig",
            "note": "drop me", "run_id": "wrong", "fence": 1}


def define(**overrides):
    case = {"run_as": [], "run_id": "run-1", "fence": 7, "account": "claude-01", "provider": "claude",
            "model": "opus", "effort": "high", "node": "cc", "control_url": "http://control.test",
            "token": "tok-1", "heartbeat_s": 30, "snapshot": snap(), "flow_bugs": {"reports": []},
            "responses": [], "action_script": [], "heartbeats": [], "time0": 1_700_000_000.0, "time_step": 0.0,
            "mono0": 5_000.0, "mono_step": 0.0, "chown": "ok", "git_exists": False, "git_error": None,
            "context_error": None, "context_message": "stale context", "flow_error": None,
            "flow_message": "stale flow", "plan_error": None, "plan_message": "stale plan",
            "lose_after_plan": False, "use_real_adapter": False, "context_pack": {}, "flow_bugs_value": None}
    case.update(overrides)
    return case


CASES = {
    "idle_empty": define(time_step=10.5),
    "idle_negative_duration": define(time_step=-1.5),
    "idle_warn_clock": define(snapshot=snap(running_clocks=[{"passed": ["warn"]}])),
    "idle_null_passed": define(snapshot=snap(running_clocks=[{"passed": None}])),
    "idle_reports_overwritten": define(snapshot=snap(flow_bug_reports=[{"old": 1}])),
    "idle_flow_bugs_without_reports_key": define(flow_bugs={}),
    "idle_chown_permission": define(run_as=RUN_AS, chown="permission"),
    "idle_chown_lookup": define(run_as=RUN_AS, chown="lookup"),
    "idle_git_exists": define(git_exists=True),
    "chown_oserror": define(run_as=RUN_AS, chown="oserror"),
    "git_missing": define(git_error="missing"),
    "success_empty_plan": define(snapshot=incident_snapshot(), responses=[ok()], run_as=RUN_AS, chown="ok",
                                 time_step=10.5),
    "review_needs_owner": define(snapshot=snap(needs_owner=[{"key": "T-1"}]), responses=[ok()]),
    "review_offline_nodes": define(snapshot=snap(offline_nodes=["n1"]), responses=[ok()]),
    "review_quota_accounts": define(snapshot=snap(quota_accounts=[{"account": "a"}]), responses=[ok()]),
    "review_quota_incidents": define(snapshot=snap(quota_incidents=[{"kind": "quota"}]), responses=[ok()]),
    "review_flow_bug_reports": define(flow_bugs={"reports": [{"signature": "s"}]}, responses=[ok()]),
    "review_hard_clock": define(snapshot=snap(running_clocks=[{"passed": ["warn", "hard"]}]), responses=[ok()]),
    "review_hard_substring": define(snapshot=snap(running_clocks=[{"passed": "hard-limit"}]), responses=[ok()]),
    "review_truthy_string_incident": define(snapshot=snap(open_incidents="x"), responses=[ok()]),
    "timeout_zero": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 0}),
                           responses=[ok()]),
    "timeout_negative": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": -4}),
                               responses=[ok()]),
    "timeout_one": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 1}),
                          responses=[ok()]),
    "timeout_600": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 600}),
                          responses=[ok()]),
    "timeout_601": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 601}),
                          responses=[ok()]),
    "timeout_float_low": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 1.9}),
                                responses=[ok()]),
    "timeout_float_high": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 600.9}),
                                 responses=[ok()]),
    "timeout_true": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": True}),
                           responses=[ok()]),
    "timeout_numeric_string": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": "601"}),
                                     responses=[ok()]),
    "timeout_remaining_none": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": None}),
                                     responses=[ok()]),
    "quota_error_ignores_reset": define(
        snapshot=incident_snapshot(), mono_step=0.5,
        responses=[err("quota", message="limited", tokens_in=4, tokens_out=5, cost_usd=2, reset_at="reset",
                       quota={"five_hour": {"used": 1}}, quota_observed_at="observed")]),
    "auth_error": define(snapshot=incident_snapshot(), responses=[err("auth", message="signed out")]),
    "timeout_error_truncated": define(snapshot=incident_snapshot(),
                                      responses=[err("timeout", message="M" * 401)]),
    "network_error": define(snapshot=incident_snapshot(), responses=[err("network", message="down")]),
    "quota_without_observed_at": define(
        snapshot=incident_snapshot(),
        responses=[err("quota", quota={"five_hour": {"used": 2}}, quota_observed_at=None)]),
    "bad_quota_observed_at": define(
        snapshot=incident_snapshot(),
        responses=[err("quota", quota={"five_hour": {"used": 1}}, quota_observed_at="bad")]),
    "error_message_none": define(snapshot=incident_snapshot(), responses=[err("network", message=None)]),
    "retry_then_valid": define(
        snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 601}), mono_step=0.5,
        responses=[ok(text="not json", progress=[{"type": "x" * 50}], quota={"five_hour": {"used": 1}},
                      quota_observed_at="observed", tokens_in=2, tokens_out=3, cost_usd=1),
                   ok(text=plan("retried"), progress=[{"type": "done"}], tokens_in=4, tokens_out=5, cost_usd=2,
                      quota={})]),
    "retry_still_invalid": define(
        snapshot=incident_snapshot(),
        responses=[ok(text="not json", tokens_in=1, tokens_out=1, cost_usd=1),
                   ok(text="Z" * 1001, tokens_in=3, tokens_out=4, cost_usd=2)]),
    "retry_skipped": define(
        snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": 0}), mono_step=5,
        responses=[ok(text="not json", tokens_in=6, tokens_out=7, cost_usd=3)]),
    "retry_quota_error": define(
        snapshot=incident_snapshot(),
        responses=[ok(text="not json", tokens_in=1, tokens_out=1, cost_usd=1, quota={}),
                   err("quota", message="later", tokens_in=8, tokens_out=9, cost_usd=4, reset_at="reset",
                       quota={"seven_day": {"used": 3}}, quota_observed_at="observed")]),
    "retry_auth_error": define(
        snapshot=incident_snapshot(),
        responses=[ok(text="not json"), err("auth", message="later", tokens_in=1, tokens_out=1, cost_usd=1)]),
    "retry_timeout_truncated": define(
        snapshot=incident_snapshot(),
        responses=[ok(text="not json", tokens_in=1, tokens_out=1, cost_usd=1),
                   err("timeout", message="T" * 450, tokens_in=2, tokens_out=2, cost_usd=1)]),
    "retry_provider_error": define(
        snapshot=incident_snapshot(),
        responses=[ok(text="not json"), err("provider", message="backend", tokens_in=1, tokens_out=1, cost_usd=1)]),
    "lost_during_model": define(snapshot=incident_snapshot(), responses=[ok(set_lost=True)]),
    "lost_during_retry": define(
        snapshot=incident_snapshot(),
        responses=[ok(text="not json"), err("quota", message="ignored", set_lost=True)]),
    "lost_heartbeat_conflict": define(snapshot=incident_snapshot(), responses=[ok()], heartbeats=["conflict"]),
    "lost_before_actions": define(
        snapshot=incident_snapshot(), lose_after_plan=True,
        responses=[ok(text=plan("ready", [{"action": "retry", "intent": "go"}]))]),
    "lost_between_actions": define(
        snapshot=incident_snapshot(),
        responses=[ok(text=plan("ready", [{"action": "retry", "intent": "one"},
                                          {"action": "move_to_todo", "intent": "two"}]))],
        action_script=[{"escalated": False, "then_lost": True}, {"escalated": True}]),
    "lost_on_last_action": define(
        snapshot=incident_snapshot(),
        responses=[ok(text=plan("ready", [{"action": "retry", "intent": "only"}]))],
        action_script=[{"escalated": True, "then_lost": True}]),
    "conflict_context": define(context_error="conflict", context_message="context fence"),
    "conflict_flow_bugs": define(flow_error="conflict", flow_message="flow fence"),
    "conflict_plan_outcome": define(snapshot=incident_snapshot(), responses=[ok()], plan_error="conflict",
                                    plan_message="plan fence"),
    "conflict_on_action": define(
        snapshot=incident_snapshot(),
        responses=[ok(text=plan("ready", [{"action": "retry", "intent": "one"},
                                          {"action": "pause_account", "intent": "two"}]))],
        action_script=[{"escalated": False}, {"error": "conflict", "message": "action fence"}]),
    "mixed_actions": define(
        snapshot=incident_snapshot(),
        responses=[ok(text=plan("did the safe ones", [
            allowed_action(),
            {"action": "retry", "intent": "plain"},
            {"action": "escalate_to_owner", "intent": "ask", "ticket": "T-2"},
            {"action": "retry", "intent": "string-false"},
            {"action": "retry", "intent": "zero"},
            {"action": "retry", "intent": "two"},
            {"action": "retry", "intent": "none"},
            {"action": "move_to_todo", "intent": "list"},
            {"action": "pause_account", "intent": "boom"},
            {"action": "retry", "intent": "last"},
        ]))],
        action_script=[
            {"escalated": True},
            {},
            {"escalated": True},
            {"escalated": "false"},
            {"escalated": 0},
            {"escalated": 2},
            {"return": None},
            {"return": [1]},
            {"error": "runtime", "message": "denied by policy"},
            {"escalated": False},
        ]),
    "worker_runtime": define(context_error="runtime", context_message="disk full"),
    "worker_truncated": define(context_error="runtime", context_message="E" * 2000),
    "worker_bad_clock": define(snapshot=snap(running_clocks=["hard"])),
    "worker_len_incidents": define(snapshot=snap(open_incidents=3)),
    "worker_no_adapter": define(snapshot=incident_snapshot(), provider="missing", use_real_adapter=True),
    "worker_bad_reset_at": define(snapshot=incident_snapshot(), responses=[ok(reset_at="bad")]),
    "worker_bad_remaining": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy={"remaining_seconds": "nope"})),
    "worker_autonomy_not_dict": define(snapshot=snap(open_incidents=[{"k": 1}], autonomy=5)),
    "worker_flow_bugs_none": define(snapshot=incident_snapshot(), flow_bugs_value="none"),
    "knowledge_prompt": define(
        snapshot=incident_snapshot(), responses=[ok()], context_pack={"items": [
            {"kind": "study_case", "scope": "project", "project": "P", "title": "Keep", "content": "body",
             "status": "active"}]},
        account="grok-9", provider="grok", model="grok-4", effort="medium", node="node9", fence=11, run_id="run-9",
        control_url="http://control.example/base", token="tok-9"),
    "inbox_continuation": define(
        snapshot=incident_snapshot(),
        heartbeats=[{"inbox": [{"id": 9, "kind": "context", "author": "Owner", "body": "note"}]}],
        responses=[ok(text="not json", tokens_in=2, tokens_out=3, cost_usd=1.5, command_results=[],
                      permission_denials=0, duration_s=0.0),
                   ok(text=plan("continued"), tokens_in=5, tokens_out=7, cost_usd=2.5, command_results=[],
                      permission_denials=0, duration_s=0.0)]),
    "checkpoint_boom_continues": define(snapshot=incident_snapshot(), responses=[ok()], heartbeats=["boom"]),
    "long_summary": define(snapshot=incident_snapshot(), responses=[ok(text=plan("S" * 1200))]),
    "whitespace_json": define(
        snapshot=incident_snapshot(), model="sonnet", effort="low",
        responses=[ok(text='  {"summary": "ok", "actions": []}  ')]),
}


def _resolve_marker(value, observed=False):
    if value == "reset":
        return RESET_AT
    if value == "observed":
        return OBSERVED_AT
    if value == "bad":
        return 1 if observed else "tomorrow"
    return value


def _namespace(item, spec):
    return SimpleNamespace(
        tokens_in=item.get("tokens_in", 0), tokens_out=item.get("tokens_out", 0),
        cost_usd=item.get("cost_usd", 0), quota=item.get("quota") or {},
        error_kind=item.get("error_kind", ""), error_message=item.get("error_message", ""),
        reset_at=_resolve_marker(item.get("reset_at")), final_text=item.get("final_text", ""),
        log_path=spec.log_path, quota_observed_at=_resolve_marker(item.get("quota_observed_at"), observed=True),
        permission_denials=item.get("permission_denials", 0), duration_s=item.get("duration_s", 0.0),
        command_results=list(item.get("command_results") or []),
        token_usage_reported=item.get("token_usage_reported", False), verdict=item.get("verdict", ""),
        verdict_reason=item.get("verdict_reason", ""), exit_code=item.get("exit_code", 0),
        credential_expires_at=None)


def _spec_view(spec, srv: str) -> dict:
    prompt = spec.prompt
    return {"cwd": _norm(spec.cwd, srv), "account_home": _norm(spec.account_home, srv),
            "model": spec.model, "effort": spec.effort, "timeout_s": spec.timeout_s, "idle_s": spec.idle_s,
            "control_url": spec.control_url, "run_token": spec.run_token, "log_path": _norm(spec.log_path, srv),
            "log_append": spec.log_append, "extra_env": spec.extra_env, "sandbox": spec.sandbox,
            "hook": spec.container_start_hook is not None,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "prompt_len": len(prompt),
            "retry": REMINDER in prompt, "knowledge": "Knowledge context pack" in prompt}


def _norm(value, srv: str):
    if isinstance(value, str):
        return value.replace(srv, "{srv}")
    if isinstance(value, dict):
        return {str(key): _norm(item, srv) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(item, srv) for item in value]
    if isinstance(value, Path):
        return _norm(str(value), srv)
    return value


class _Clock:
    def __init__(self, case):
        self.time0 = case["time0"]
        self.time_step = case["time_step"]
        self.mono0 = case["mono0"]
        self.mono_step = case["mono_step"]
        self.time_calls = 0
        self.mono_calls = 0

    def time(self):
        value = self.time0 + self.time_calls * self.time_step
        self.time_calls += 1
        return value

    def monotonic(self):
        value = self.mono0 + self.mono_calls * self.mono_step
        self.mono_calls += 1
        return value


class _Chown:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def chown(self, path, user, group):
        self.calls.append({"path": str(path), "user": user, "group": group})
        if self.mode == "permission":
            raise PermissionError("not permitted")
        if self.mode == "lookup":
            raise LookupError("unknown user")
        if self.mode == "oserror":
            raise OSError("chown failed")


class _Git:
    def __init__(self, error):
        self.error = error
        self.calls = []

    def run(self, argv, capture_output=None, timeout=None):
        self.calls.append({"argv": [str(part) for part in argv], "capture_output": capture_output,
                           "timeout": timeout})
        if self.error == "missing":
            raise FileNotFoundError("git missing")
        return SimpleNamespace(returncode=0)


class _Api:
    def __init__(self, case, holder):
        self.case = case
        self.holder = holder
        self.calls = []
        self.responses = [dict(item) for item in case["responses"]]
        self.actions = [dict(item) for item in case["action_script"]]
        self.heartbeats = list(case["heartbeats"])

    def call(self, method, path, json=None, params=None, retries=4, allow_not_found=False):
        self.calls.append({"method": method, "path": path, "json": copy.deepcopy(json),
                           "params": copy.deepcopy(params)})
        pair = (method, path)
        if pair == ("GET", "/v1/master/context"):
            self._raise(self.case["context_error"], self.case["context_message"])
            return copy.deepcopy(self.case["snapshot"])
        if pair == ("POST", "/v1/master/flow-bugs"):
            self._raise(self.case["flow_error"], self.case["flow_message"])
            if self.case["flow_bugs_value"] == "none":
                return None
            return copy.deepcopy(self.case["flow_bugs"])
        if path.endswith("/heartbeat"):
            return self._heartbeat()
        if pair == ("POST", "/v1/master/plan-outcome"):
            self._raise(self.case["plan_error"], self.case["plan_message"])
            if self.case["lose_after_plan"]:
                self.holder[0].lost.set()
            return {"valid": json["valid"]}
        if pair == ("POST", "/v1/master/actions"):
            return self._action()
        raise ScriptError(f"unexpected call {method} {path}")

    def _raise(self, kind, message):
        if kind == "conflict":
            raise Conflict(message)
        if kind == "runtime":
            raise RuntimeError(message)

    def _heartbeat(self):
        item = self.heartbeats.pop(0) if self.heartbeats else {"inbox": []}
        if item == "conflict":
            raise Conflict("heartbeat refused")
        if item == "boom":
            raise RuntimeError("hb down")
        return item

    def _action(self):
        if not self.actions:
            raise ScriptError("action without a script")
        script = self.actions.pop(0)
        if script.get("error") == "conflict":
            raise Conflict(script.get("message", "conflict"))
        if script.get("error") == "runtime":
            raise RuntimeError(script.get("message", "refused"))
        if script.get("then_lost"):
            self.holder[0].lost.set()
        if "return" in script:
            return script["return"]
        body = {}
        if "escalated" in script:
            body["escalated"] = script["escalated"]
        return body


class _Adapter:
    def __init__(self, responses):
        self.responses = responses
        self.specs = []

    def argv(self, spec):
        return ["codex-bin"]

    def run(self, spec, on_progress=None, stop=None, on_start=None):
        self.specs.append(spec)
        if on_start:
            on_start()
        if not self.responses:
            raise ScriptError("adapter run without a scripted response")
        item = self.responses.pop(0)
        for event in item.get("progress") or []:
            on_progress(event)
        if item.get("set_lost"):
            stop.set()
        return _namespace(item, spec)


def execute(tmp_path: Path, case: dict) -> dict:
    """Run one scripted master cycle and return the normalized trace."""
    case = copy.deepcopy(case)
    srv = str(tmp_path)
    if case["git_exists"]:
        (tmp_path / "work" / ".master" / ".git").mkdir(parents=True)
    clock = _Clock(case)
    chown = _Chown(case["chown"])
    git = _Git(case["git_error"])
    holder: list = []
    api = _Api(case, holder)
    adapter = _Adapter(api.responses)
    providers: list[str] = []
    records: list[str] = []

    class SpyHeartbeat(Heartbeat):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.did_start = False
            holder.append(self)

        def start(self):
            self.did_start = True
            super().start()

    class _Log(logging.Handler):
        def emit(self, record):
            exc = ""
            if record.exc_info and record.exc_info[0]:
                exc = " exc=" + record.exc_info[0].__name__
            records.append(f"{record.levelname} {record.name} {record.getMessage()}{exc}")

    handler = _Log()
    loggers = [logging.getLogger(name) for name in ("aiflow4.master", "aiflow4.dev")]
    previous = [(item, item.level) for item in loggers]
    originals = {"time": worker_master.time, "shutil": worker_master.shutil,
                 "subprocess": worker_master.subprocess, "get_adapter": worker_master.get_adapter,
                 "Heartbeat": worker_master.Heartbeat}
    claimed = Claimed(run_id=case["run_id"], fence=case["fence"], run_token=case["token"], account=case["account"],
                      provider=case["provider"], model=case["model"], effort=case["effort"],
                      ticket={"key": "master:global"}, project={"node": case["node"]},
                      identity=("bot", "bot@example.test"), step="master", context_pack=case["context_pack"])
    try:
        for logger, _level in previous:
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        worker_master.time = clock
        worker_master.shutil = chown
        worker_master.subprocess = git
        worker_master.Heartbeat = SpyHeartbeat
        if not case["use_real_adapter"]:
            worker_master.get_adapter = lambda name: providers.append(name) or adapter
        try:
            result = worker_master.run_master(api, claimed, srv, case["control_url"], case["heartbeat_s"],
                                               case["run_as"])
            raised = None
        except ScriptError:
            raise
        except Exception as exc:
            result = None
            raised = {"type": type(exc).__name__, "message": str(exc)}
        heartbeat = holder[0]
        trace = {"result": result, "raised": raised, "calls": api.calls,
                 "runs": [_spec_view(spec, srv) for spec in adapter.specs],
                 "argv": adapter.argv("probe") if providers else None,
                 "providers": providers, "git": git.calls, "chown": chown.calls, "logs": list(records),
                 "progress": heartbeat.progress, "stop": heartbeat.stop.is_set(),
                 "started": heartbeat.did_start, "heartbeat_s": heartbeat.every,
                 "time_calls": clock.time_calls, "mono_calls": clock.mono_calls}
        return _norm(trace, srv)
    finally:
        for name, value in originals.items():
            setattr(worker_master, name, value)
        for logger, level in previous:
            logger.removeHandler(handler)
            logger.setLevel(level)
        for heartbeat in holder:
            if heartbeat.did_start:
                heartbeat.stop.set()
                heartbeat.join(timeout=2)


def load_traces() -> dict:
    traces = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        traces[item["name"]] = item["trace"]
    return traces


@pytest.mark.parametrize("name", list(CASES))
def test_run_master_matches_recorded_trace(tmp_path, name):
    assert execute(tmp_path, CASES[name]) == load_traces()[name]


def test_recorded_traces_cover_the_case_table():
    assert set(load_traces()) == set(CASES)


def _claim() -> Claimed:
    return Claimed(run_id="run-1", fence=7, run_token="tok-1", account="claude-01", provider="claude",
                   model="opus", effort="high", ticket={"key": "master:global"}, project={"node": "cc"},
                   identity=("bot", "bot@example.test"), step="master")


def test_prepare_master_workspace_creates_dirs_and_limits_chown_errors(tmp_path, monkeypatch):
    seen = {"chown": [], "git": [], "mode": "permission"}

    class _Shutil:
        def chown(self, path, user, group):
            seen["chown"].append((user, group))
            if seen["mode"] == "permission":
                raise PermissionError("no")
            if seen["mode"] == "lookup":
                raise LookupError("missing-user")
            raise OSError("disk")

    class _Git:
        def run(self, argv, capture_output, timeout):
            seen["git"].append(list(argv))
            return SimpleNamespace(returncode=9)

    monkeypatch.setattr(worker_master, "shutil", _Shutil())
    monkeypatch.setattr(worker_master, "subprocess", _Git())
    work, logs = worker_master.prepare_master_workspace(str(tmp_path), ["runuser", "--"])
    assert work == tmp_path / "work" / ".master" and logs == tmp_path / "logs" / "runs"
    assert work.is_dir() and logs.is_dir()
    assert seen["chown"] == [("aiflow-worker", "aiflow-worker")]
    assert seen["git"] == [["runuser", "--", "git", "init", "-q", str(work)]]
    (work / ".git").mkdir()
    seen["mode"] = "lookup"
    worker_master.prepare_master_workspace(str(tmp_path), [])
    assert len(seen["chown"]) == 1 and len(seen["git"]) == 1
    seen["mode"] = "os"
    with pytest.raises(OSError, match="disk"):
        worker_master.prepare_master_workspace(str(tmp_path / "other"), ["runuser"])


def test_load_master_snapshot_overwrites_reports():
    class _Api:
        def __init__(self, bugs):
            self.bugs, self.calls = bugs, []

        def call(self, method, path, body=None, params=None):
            self.calls.append((method, path, body, params))
            if method == "GET":
                return {"flow_bug_reports": ["old"]}
            return self.bugs

    claimed = _claim()
    api = _Api({"reports": [{"id": 1}]})
    snapshot = worker_master.load_master_snapshot(api, claimed)
    assert snapshot["flow_bug_reports"] == [{"id": 1}]
    assert api.calls[0] == ("GET", "/v1/master/context", None, {"run_id": "run-1", "fence": 7})
    assert api.calls[1] == ("POST", "/v1/master/flow-bugs", {"run_id": "run-1", "fence": 7}, None)
    assert worker_master.load_master_snapshot(_Api({}), claimed)["flow_bug_reports"] == []
    with pytest.raises(AttributeError):
        worker_master.load_master_snapshot(_Api(None), claimed)


def test_master_review_progress_counts_snapshot_lists():
    progress = worker_master.master_review_progress(
        {"open_incidents": "x", "needs_owner": [], "running_clocks": [1, 2], "flow_bug_reports": []})
    assert progress == {"phase": "master-review", "incidents": 1, "needs_owner": 0, "running": 2, "flow_bugs": 0}


def test_finish_idle_master_truncates_duration_toward_zero(monkeypatch):
    result = {"outcome": "failed", "details": {}}
    monkeypatch.setattr(worker_master, "time", SimpleNamespace(time=lambda: 100.5))
    worker_master.finish_idle_master(result, 90.0)
    assert result["outcome"] == "done"
    assert result["summary"] == "no incidents, owner decisions, failures or overdue runs need review"
    assert result["details"] == {"no_watch": True, "model_time_s": 0, "duration_s": 10, "log": ""}
    negative = {}
    monkeypatch.setattr(worker_master, "time", SimpleNamespace(time=lambda: 88.5))
    worker_master.finish_idle_master(negative, 90.0)
    assert negative["details"]["duration_s"] == -1


def test_master_model_timeout_caps_the_autonomy_budget():
    assert worker_master.master_model_timeout({}) == 600
    assert worker_master.master_model_timeout({"autonomy": {}}) == 600
    assert worker_master.master_model_timeout({"autonomy": {"remaining_seconds": None}}) == 600
    assert worker_master.master_model_timeout({"autonomy": {"remaining_seconds": 0}}) == 1
    assert worker_master.master_model_timeout({"autonomy": {"remaining_seconds": -4}}) == 1
    assert worker_master.master_model_timeout({"autonomy": {"remaining_seconds": True}}) == 1
    assert worker_master.master_model_timeout({"autonomy": {"remaining_seconds": 1.9}}) == 1
    assert worker_master.master_model_timeout({"autonomy": {"remaining_seconds": 600.9}}) == 600
    assert worker_master.master_model_timeout({"autonomy": {"remaining_seconds": "601"}}) == 600
    with pytest.raises(ValueError):
        worker_master.master_model_timeout({"autonomy": {"remaining_seconds": "nope"}})
    with pytest.raises(AttributeError):
        worker_master.master_model_timeout({"autonomy": 5})


def test_build_master_run_spec_is_read_only(tmp_path):
    logs = tmp_path / "logs"
    spec = worker_master.build_master_run_spec(
        _claim(), {"open_incidents": []}, tmp_path / "work", str(tmp_path), "http://control.test", logs, 42)
    assert spec.sandbox == "read-only" and spec.timeout_s == 42
    assert spec.cwd == str(tmp_path / "work")
    assert spec.account_home == str(tmp_path / "accounts" / "claude-01")
    assert spec.log_path == str(logs / "master-run-1.jsonl")
    assert spec.model == "opus" and spec.effort == "high" and spec.run_token == "tok-1"
    assert "claude-01" in spec.prompt


def test_prefixed_master_argv_keeps_the_adapter_arguments():
    argv = worker_master.prefixed_master_argv(["runuser", "--"], lambda current: ["codex", current], "probe")
    assert argv == ["runuser", "--", "codex", "probe"]


def test_bind_master_argv_replaces_the_adapter_command():
    adapter = SimpleNamespace(argv=lambda current: ["codex-bin", current])
    worker_master.bind_master_argv(adapter, ["runuser"])
    assert adapter.argv("probe") == ["runuser", "codex-bin", "probe"]


def test_record_master_ai_progress_counts_and_truncates(monkeypatch):
    monkeypatch.setattr(worker_master, "time", SimpleNamespace(time=lambda: 110.0))
    beat = SimpleNamespace(progress={})
    events = {"n": 0}
    worker_master.record_master_ai_progress(beat, events, 100.0, {"type": "x" * 50})
    assert events["n"] == 1
    assert beat.progress == {"phase": "master-ai", "events": 1, "last": "x" * 40, "elapsed_s": 10}


def test_master_quota_fields_keeps_the_latest_truthy_quota():
    older = SimpleNamespace(quota={"left": 1}, quota_observed_at=OBSERVED_AT)
    empty = SimpleNamespace(quota={}, quota_observed_at=OBSERVED_AT)
    assert worker_master.master_quota_fields([older, empty]) == {
        "quota": {"left": 1}, "quota_observed_at": OBSERVED_AT.isoformat()}
    missing = SimpleNamespace(quota={"left": 2})
    assert worker_master.master_quota_fields([missing])["quota_observed_at"] is None
    assert worker_master.master_quota_fields([SimpleNamespace(quota={})]) == {}
    bad = SimpleNamespace(quota={"left": 3}, quota_observed_at=1)
    with pytest.raises(AttributeError):
        worker_master.master_quota_fields([bad])


def test_finish_master_model_error_omits_reset_at(monkeypatch):
    monkeypatch.setattr(worker_master, "time", SimpleNamespace(monotonic=lambda: 6.0))
    auth = SimpleNamespace(error_kind="auth", error_message="later", tokens_in=4, tokens_out=5, cost_usd=1,
                           reset_at=RESET_AT)
    result = {}
    worker_master.finish_master_model_error(result, auth, _claim(), {"quota": {"a": 1}}, 4.0)
    assert "reset_at" not in result
    assert result["outcome"] == "quota"
    assert result["summary"] == "master account claude-01 cannot work now (auth)"
    assert result["tokens_in"] == 4 and result["details"] == {"model_time_s": 2}
    timed = SimpleNamespace(error_kind="timeout", error_message="M" * 401, tokens_in=1, tokens_out=1, cost_usd=0,
                            reset_at=RESET_AT)
    stored = {}
    worker_master.finish_master_model_error(stored, timed, _claim(), {}, 4.0)
    assert stored["outcome"] == "timeout"
    assert stored["summary"] == "master cycle failed: " + "M" * 400
    assert "reset_at" not in stored
    broken = SimpleNamespace(error_kind="network", error_message=None, tokens_in=1, tokens_out=1, cost_usd=0)
    untouched = {"outcome": "failed", "details": {}}
    with pytest.raises(TypeError):
        worker_master.finish_master_model_error(untouched, broken, _claim(), {}, 4.0)
    assert untouched == {"outcome": "failed", "details": {}}


def test_finish_master_retry_error_sums_tokens_and_records_reset(monkeypatch):
    monkeypatch.setattr(worker_master, "time", SimpleNamespace(monotonic=lambda: 8.0))
    first = SimpleNamespace(tokens_in=2, tokens_out=3, cost_usd=1)
    second = SimpleNamespace(tokens_in=4, tokens_out=5, cost_usd=1.5, error_kind="quota", error_message="later",
                             reset_at=RESET_AT)
    result = {}
    worker_master.finish_master_retry_error(result, [first, second], second, _claim(), {"quota": {"q": 1}}, 6.0)
    assert result["tokens_in"] == 6 and result["tokens_out"] == 8 and result["cost_usd"] == 2.5
    assert result["outcome"] == "quota" and result["reset_at"] == RESET_AT.isoformat()
    assert result["summary"] == "master account claude-01 cannot work now (quota)"
    assert result["details"] == {"plan_attempts": 2, "model_time_s": 2}
    bad = SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=1, error_kind="quota", error_message="x",
                          reset_at="tomorrow")
    untouched = {"outcome": "failed"}
    with pytest.raises(AttributeError):
        worker_master.finish_master_retry_error(untouched, [bad], bad, _claim(), {}, 6.0)
    assert untouched == {"outcome": "failed"}
    timed = SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0, error_kind="timeout", error_message="T" * 450,
                            reset_at=None)
    stored = {}
    worker_master.finish_master_retry_error(stored, [timed], timed, _claim(), {}, 6.0)
    assert stored["outcome"] == "timeout" and stored["reset_at"] is None
    assert stored["summary"] == "master cycle failed during strict-format retry: " + "T" * 400


def test_retry_invalid_master_plan_skips_a_spent_budget_and_stops_on_failure(monkeypatch):
    class _Clock:
        def __init__(self):
            self.value = 0.0

        def monotonic(self):
            self.value += 0.4
            return self.value

    clock = _Clock()
    scripted, seen = [], []
    monkeypatch.setattr(worker_master, "time", clock)
    monkeypatch.setattr(worker_master, "run_with_inbox", lambda adapter, spec, heartbeat, on_progress: (
        seen.append(spec) or scripted.pop(0)))
    spec = worker_master.RunSpec(prompt="prompt", cwd=".", account_home=".", timeout_s=600)
    original = SimpleNamespace(final_text="bad", quota={})
    skipped = worker_master.retry_invalid_master_plan(
        object(), spec, SimpleNamespace(lost=threading.Event()), None, {}, [], original, _claim(), {}, 1, 0)
    assert skipped == (None, original, {}, None) and seen == []

    clock.value = 0.0
    scripted.append(SimpleNamespace(final_text=plan(summary="retried"), error_kind="", tokens_in=1, tokens_out=1,
                                    cost_usd=0, quota={}, reset_at=None))
    parsed, latest, quota, finished = worker_master.retry_invalid_master_plan(
        object(), spec, SimpleNamespace(lost=threading.Event()), None, {}, [SimpleNamespace(quota={})], original,
        _claim(), {}, 600, 0)
    assert finished is None and parsed["summary"] == "retried" and latest.error_kind == "" and quota == {}
    assert seen[0].timeout_s == 599 and seen[0].prompt.startswith("prompt\n\nSTRICT FORMAT RETRY:")

    clock.value = 0.0
    lost = threading.Event()
    lost.set()
    scripted.append(SimpleNamespace(error_kind="quota", quota={}))
    with pytest.raises(worker_master.LeaseLost):
        worker_master.retry_invalid_master_plan(
            object(), spec, SimpleNamespace(lost=lost), None, {}, [], original, _claim(), {}, 600, 0)

    clock.value = 0.0
    failed = SimpleNamespace(final_text="", error_kind="auth", error_message="no", tokens_in=3, tokens_out=4,
                             cost_usd=1, quota={"q": 1}, quota_observed_at=None, reset_at=RESET_AT)
    scripted.append(failed)
    first = SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=1, quota={})
    result = {"outcome": "failed", "details": {}}
    parsed, latest, quota, finished = worker_master.retry_invalid_master_plan(
        object(), spec, SimpleNamespace(lost=threading.Event()), None, result, [first], original, _claim(), {},
        600, 0)
    assert parsed is None and finished is result and latest is failed
    assert result["outcome"] == "quota" and result["tokens_in"] == 4 and result["tokens_out"] == 5
    assert result["reset_at"] == RESET_AT.isoformat() and result["details"]["plan_attempts"] == 2


def test_record_master_usage_sums_responses_and_keeps_a_bad_reset_off_the_result():
    first = SimpleNamespace(tokens_in=2, tokens_out=3, cost_usd=1)
    second = SimpleNamespace(tokens_in=4, tokens_out=5, cost_usd=2, error_kind="", reset_at=None)
    result = {}
    worker_master.record_master_usage(result, [first, second], second, {"quota": {"q": 1}})
    assert result["tokens_in"] == 6 and result["tokens_out"] == 8 and result["cost_usd"] == 3
    assert result["error_kind"] == "" and result["reset_at"] is None and result["quota"] == {"q": 1}
    bad = SimpleNamespace(error_kind="", reset_at="tomorrow")
    untouched = {"kept": True}
    with pytest.raises(AttributeError):
        worker_master.record_master_usage(untouched, [first], bad, {})
    assert untouched == {"kept": True}


def test_master_action_payload_drops_unknown_keys_and_stamps_the_fence():
    payload = worker_master.master_action_payload(allowed_action(), _claim())
    assert "note" not in payload
    assert payload["run_id"] == "run-1" and payload["fence"] == 7
    assert payload["action"] == "retry" and payload["signature"] == "sig"


def test_perform_master_actions_counts_refusals_and_reraises_conflict(monkeypatch, caplog):
    monkeypatch.setattr(worker_master, "time", SimpleNamespace(time=lambda: 110.0))
    script = [None, {"escalated": "false"}, {"escalated": 0}, RuntimeError("denied")]

    class _Api:
        def __init__(self, items):
            self.items, self.calls = items, []

        def call(self, method, path, body=None, params=None):
            self.calls.append(body.get("action", ""))
            item = self.items.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    response = SimpleNamespace(log_path="/tmp/master.jsonl")
    actions = [{"action": "a"}, {"action": "b"}, {"action": "c"}, {"action": "d"}]
    with caplog.at_level(logging.WARNING, logger="aiflow4.master"):
        done = worker_master.perform_master_actions(
            _Api(script), _claim(), SimpleNamespace(lost=threading.Event()), {},
            {"summary": "checked", "actions": actions}, 3, 100.0, response)
    assert done["outcome"] == "done" and done["summary"] == "checked"
    assert done["details"] == {"actions": 4, "completed": 3, "escalated": 1, "refused": 2, "model_time_s": 3,
                               "duration_s": 10, "log": "/tmp/master.jsonl"}
    assert "master action refused for run run-1 (a):" in caplog.text
    assert "master action refused for run run-1 (d): denied" in caplog.text
    lost = threading.Event()
    lost.set()
    with pytest.raises(worker_master.LeaseLost):
        worker_master.perform_master_actions(
            _Api([{"escalated": True}]), _claim(), SimpleNamespace(lost=lost), {},
            {"summary": "x", "actions": [{"action": "late"}]}, 1, 100.0, response)
    stored = {"details": {}}
    with pytest.raises(Conflict):
        worker_master.perform_master_actions(
            _Api([{"escalated": True}, Conflict("stale")]), _claim(), SimpleNamespace(lost=threading.Event()),
            stored, {"summary": "x", "actions": [{}, {}]}, 1, 100.0, response)
    assert stored == {"details": {}}


def test_apply_master_plan_reports_validity_before_actions(monkeypatch):
    monkeypatch.setattr(worker_master, "time", SimpleNamespace(monotonic=lambda: 4.0, time=lambda: 10.0))
    calls = []

    class _Api:
        def __init__(self, answer):
            self.answer = answer

        def call(self, method, path, body=None, params=None):
            calls.append((method, path, body))
            return self.answer

    response = SimpleNamespace(final_text="Z" * 1001, error_kind="", reset_at=None, tokens_in=2, tokens_out=3,
                               cost_usd=1, log_path="log.jsonl", quota={})
    invalid = worker_master.apply_master_plan(
        _Api({}), _claim(), SimpleNamespace(lost=threading.Event()), {}, None, [response], response, {}, 3.0, 0.0)
    assert calls == [("POST", "/v1/master/plan-outcome", {"run_id": "run-1", "fence": 7, "valid": False})]
    assert invalid["summary"] == "master returned no valid JSON action plan after the strict-format retry"
    assert invalid["details"]["output"] == "Z" * 1000 and invalid["details"]["plan_attempts"] == 1
    assert invalid["details"]["invalid_plan"] is True and invalid["details"]["model_time_s"] == 1
    assert invalid["tokens_in"] == 2 and invalid["reset_at"] is None
    calls.clear()
    body = {"summary": "ok", "actions": [{"action": "retry", "intent": "go"}]}
    response = SimpleNamespace(final_text="{}", error_kind="", reset_at=None, tokens_in=1, tokens_out=1,
                               cost_usd=0, log_path="l", quota={})
    done = worker_master.apply_master_plan(
        _Api({"escalated": False}), _claim(), SimpleNamespace(lost=threading.Event()), {}, body, [response],
        response, {}, 3.0, 0.0)
    assert calls[0][2]["valid"] is True and calls[1][1] == "/v1/master/actions"
    assert done["outcome"] == "done" and done["details"]["completed"] == 1 and done["details"]["escalated"] == 0


def test_run_master_cycle_stays_idle_without_an_adapter_and_raises_when_the_lease_is_lost(tmp_path, monkeypatch):
    class _Api:
        def __init__(self, snapshot):
            self.snapshot, self.calls = snapshot, []

        def call(self, method, path, body=None, params=None):
            self.calls.append((method, path))
            if method == "GET":
                return self.snapshot
            return {"reports": []}

    def fail_adapter(name):
        raise AssertionError(name)

    monkeypatch.setattr(worker_master, "time", SimpleNamespace(time=lambda: 10.0, monotonic=lambda: 3.0))
    monkeypatch.setattr(worker_master, "get_adapter", fail_adapter)
    beat = SimpleNamespace(progress={}, lost=threading.Event())
    idle = worker_master.run_master_cycle(
        _Api(snap()), _claim(), beat, {"outcome": "failed", "details": {}}, tmp_path, tmp_path, str(tmp_path),
        "http://control.test", [], 0.0)
    assert idle["details"]["no_watch"] is True and idle["details"]["duration_s"] == 10
    assert beat.progress["phase"] == "master-review"

    class _Adapter:
        def __init__(self):
            self.argv = lambda current: ["bin", current]

        def run(self, spec, on_progress, stop=None, on_start=None):
            return SimpleNamespace(error_kind="quota", error_message="later", tokens_in=1, tokens_out=1,
                                   cost_usd=0, quota={"q": 1}, final_text="")

    monkeypatch.setattr(worker_master, "get_adapter", lambda name: _Adapter())
    lost = threading.Event()
    lost.set()
    stored = {"outcome": "failed", "details": {}}
    with pytest.raises(worker_master.LeaseLost):
        worker_master.run_master_cycle(
            _Api(snap(open_incidents=[{"kind": "x"}])), _claim(), SimpleNamespace(progress={}, lost=lost), stored,
            tmp_path, tmp_path, str(tmp_path), "http://control.test", [], 0.0)
    assert stored == {"outcome": "failed", "details": {}}


def test_run_master_lets_a_workspace_oserror_escape_before_the_heartbeat_starts(tmp_path, monkeypatch):
    class _Boom(OSError):
        pass

    class _Shutil:
        def chown(self, path, user, group):
            raise _Boom("disk")

    holder, started = [], []
    original = worker_master.Heartbeat

    class _Beat(original):
        def __init__(self, api, run, every):
            super().__init__(api, run, every)
            holder.append(self)

        def start(self):
            started.append(True)

    monkeypatch.setattr(worker_master, "shutil", _Shutil())
    monkeypatch.setattr(worker_master, "Heartbeat", _Beat)
    with pytest.raises(_Boom, match="disk"):
        worker_master.run_master(SimpleNamespace(), _claim(), str(tmp_path), "http://control.test", 30, ["runuser"])
    assert started == [] and holder[0].stop.is_set() is False
