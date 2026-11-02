from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.worker import loop as worker_loop
from aiflow.v4.worker.client import Conflict


DEFAULT_API = {
    "/v1/version-releases/candidates": [{"versions": []}],
    "/v1/ba-chat/candidate": [{"job": None}],
    "/v1/master/candidate": [{"candidate": None}],
    "/v1/work/candidates": [{"tickets": []}],
}
VERSION = {"project": "SHOP", "version_id": "v-next", "repository": "shop-repo"}
VERSION_CLAIM = {
    "claimed": True,
    "run_id": "run-version",
    "fence": 8,
    "run_token": "version-run-token",
    "provider": "claude",
    "ticket": {"key": "SHOP-8"},
    "project": {"key": "SHOP"},
    "identity": ["node-a", "claude-a"],
    "version": "2.4",
    "tracker_key": "SHOP-8",
    "context_pack": {"guide": "release"},
}
WORK_CLAIM = {
    "claimed": True,
    "run_id": "run-work",
    "fence": 9,
    "run_token": "work-run-token",
    "provider": "codex",
    "model": "model-a",
    "effort": "high",
    "ticket": {"key": "SHOP-9"},
    "project": {"key": "SHOP"},
    "identity": ["node-a", "codex-a"],
    "findings": "finding text",
    "branch": "feature-cycle",
    "pr": {"number": 17},
    "last_dev_summary": "prior work",
    "verify_findings": "review note",
    "recent_comments": [{"body": "comment"}],
    "triage_answer": "answer",
    "triage_question": "question",
    "related": [{"key": "SHOP-10"}],
    "project_tickets": [{"key": "SHOP-11"}],
    "context_pack": {"guide": "work"},
    "machine_tools": {"python": "3.12"},
    "owner_register": [{"name": "owner"}],
    "escalation_class": "normal",
}
BA_JOB = {"job_id": "chat-3", "claim_token": "chat-claim-token", "question": "Status?"}


def _case(name: str, api: dict | None = None, runners: dict | None = None) -> dict:
    responses = {key: list(value) for key, value in DEFAULT_API.items()}
    responses.update(api or {})
    return {"name": name, "api": responses, "runners": runners or {}}


CASES = [
    _case("idle-empty"),
    _case("idle-missing-response-fields", {
        "/v1/version-releases/candidates": [None],
        "/v1/ba-chat/candidate": [{}],
        "/v1/master/candidate": [{}],
        "/v1/work/candidates": [{"tickets": []}],
    }),
    _case("version-claim-conflict", {
        "/v1/version-releases/candidates": [{"versions": [VERSION]}],
        "/v1/version-releases/claim": [{"raises": "Conflict", "message": "claim moved"}],
    }),
    _case("version-claim-refused", {
        "/v1/version-releases/candidates": [{"versions": [VERSION]}],
        "/v1/version-releases/claim": [{"claimed": False}],
    }),
    _case("version-claim-missing-flag", {
        "/v1/version-releases/candidates": [{"versions": [VERSION]}],
        "/v1/version-releases/claim": [{}],
    }),
    _case("version-success", {
        "/v1/version-releases/candidates": [{"versions": [VERSION]}],
        "/v1/version-releases/claim": [VERSION_CLAIM],
    }, {"run_version_release": {"outcome": "released"}}),
    _case("version-result-report-error", {
        "/v1/version-releases/candidates": [{"versions": [VERSION]}],
        "/v1/version-releases/claim": [VERSION_CLAIM],
        "/v1/runs/*/result": [{"raises": "RuntimeError", "message": "result offline"}],
    }, {"run_version_release": {"outcome": "released"}}),
    _case("version-release-cleanup-error", {
        "/v1/version-releases/candidates": [{"versions": [VERSION]}],
        "/v1/version-releases/claim": [VERSION_CLAIM],
        "/v1/runs/*/release": [{"raises": "RuntimeError", "message": "lease gone"}],
    }, {"run_version_release": {"outcome": "released"}}),
    _case("version-run-error-escapes", {
        "/v1/version-releases/candidates": [{"versions": [VERSION]}],
        "/v1/version-releases/claim": [VERSION_CLAIM],
    }, {"run_version_release": {"raises": "ValueError", "message": "runner failed"}}),
    _case("version-refusal-then-next", {
        "/v1/version-releases/candidates": [{"versions": [VERSION, {**VERSION, "version_id": "v-later"}]}],
        "/v1/version-releases/claim": [{"claimed": False}, VERSION_CLAIM],
    }, {"run_version_release": {"outcome": "released"}}),
    _case("ba-chat-missing-job", {"/v1/ba-chat/candidate": [{}]}),
    _case("ba-chat-success", {"/v1/ba-chat/candidate": [{"job": BA_JOB}]}, {
        "run_ba_chat_turn": {"output": {"answer": "Hello"}, "error_kind": ""},
    }),
    _case("ba-chat-run-error", {"/v1/ba-chat/candidate": [{"job": BA_JOB}]}, {
        "run_ba_chat_turn": {"raises": "RuntimeError", "message": "provider stopped"},
    }),
    _case("ba-chat-result-error", {
        "/v1/ba-chat/candidate": [{"job": BA_JOB}],
        "/v1/ba-chat/*/result": [{"raises": "OSError", "message": "chat store failed"}],
    }, {"run_ba_chat_turn": {"output": {"answer": "Hello"}, "error_kind": ""}}),
    _case("ba-chat-empty-result-fields", {"/v1/ba-chat/candidate": [{"job": BA_JOB}]}, {
        "run_ba_chat_turn": {"output": None, "error_kind": None},
    }),
    _case("work-no-candidates"),
    _case("work-claim-conflict", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-1", "step": "dev"}]}],
        "/v1/work/claim": [{"raises": "Conflict", "message": "candidate moved"}],
    }),
    _case("work-claim-refused", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-1", "step": "dev"}]}],
        "/v1/work/claim": [{"claimed": False}],
    }),
    _case("work-claim-missing-flag", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-1", "step": "dev"}]}],
        "/v1/work/claim": [{}],
    }),
    _case("work-dev-rich", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-9", "step": "dev"}]}],
        "/v1/work/claim": [WORK_CLAIM],
    }, {"run_dev": {"outcome": "done", "details": {"log": "unused"}}}),
    _case("work-verify-sparse", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-2", "step": "verify"}]}],
        "/v1/work/claim": [{**WORK_CLAIM, "ticket": {"key": "SHOP-2"}, "pr": None,
                           "findings": None, "branch": None, "recent_comments": None}],
    }, {"run_verify": {"outcome": "verified"}}),
    _case("work-post-merge", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-3", "step": "post_merge"}]}],
        "/v1/work/claim": [WORK_CLAIM],
    }, {"run_post_merge": {"outcome": "merged"}}),
    _case("work-triage", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-4", "step": "triage"}]}],
        "/v1/work/claim": [WORK_CLAIM],
    }, {"run_triage": {"outcome": "triaged"}}),
    _case("work-master", {
        "/v1/master/candidate": [{"candidate": {"key": "SHOP-5", "step": "master"}}],
        "/v1/work/candidates": [{"tickets": []}],
        "/v1/work/claim": [WORK_CLAIM],
    }, {"run_master": {"outcome": "mastered"}}),
    _case("work-intake", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-6", "step": "intake"}]}],
        "/v1/work/claim": [WORK_CLAIM],
    }, {"run_intake": {"outcome": "intaken"}}),
    _case("work-impact", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-7", "step": "impact"}]}],
        "/v1/work/claim": [WORK_CLAIM],
    }, {"run_impact": {"outcome": "assessed"}}),
    _case("work-unknown-step-uses-dev", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-12", "step": "custom"}]}],
        "/v1/work/claim": [WORK_CLAIM],
    }, {"run_dev": {"outcome": "done"}}),
    _case("work-missing-step-defaults-to-dev", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-13"}]}],
        "/v1/work/claim": [{"claimed": True, **{key: value for key, value in WORK_CLAIM.items() if key != "step"}}],
    }, {"run_dev": {"outcome": None}}),
    _case("work-continue-after-conflict-and-refusal", {
        "/v1/work/candidates": [{"tickets": [
            {"key": "SHOP-14", "step": "dev"}, {"key": "SHOP-15", "step": "impact"},
            {"key": "SHOP-16", "step": "verify"},
        ]}],
        "/v1/work/claim": [
            {"raises": "Conflict", "message": "first moved"}, {"claimed": False},
            {**WORK_CLAIM, "ticket": {"key": "SHOP-16"}},
        ],
    }, {"run_verify": {"outcome": "verified"}}),
    _case("work-result-and-release-errors", {
        "/v1/work/candidates": [{"tickets": [{"key": "SHOP-9", "step": "dev"}]}],
        "/v1/work/claim": [WORK_CLAIM],
        "/v1/runs/*/result": [{"raises": "RuntimeError", "message": "result unavailable"}],
        "/v1/runs/*/release": [{"raises": "RuntimeError", "message": "release unavailable"}],
    }, {"run_dev": {"outcome": "done"}}),
]
CASE_BY_NAME = {case["name"]: case for case in CASES}


class _ScriptedAPI:
    def __init__(self, responses: dict, events: list) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.events = events

    def call(self, method: str, path: str, body=None, params=None, allow_not_found=False):
        self.events.append({"kind": "api", "method": method, "path": path, "body": body,
                            "params": params, "allow_not_found": allow_not_found})
        key = path
        if path.startswith("/v1/runs/"):
            key = f"/v1/runs/*/{path.rsplit('/', 1)[-1]}"
        elif path.startswith("/v1/ba-chat/") and path.endswith("/result"):
            key = "/v1/ba-chat/*/result"
        queue = self.responses.get(key, [])
        response = queue.pop(0) if queue else {}
        if isinstance(response, dict) and "raises" in response:
            error_type = response["raises"]
            error = Conflict if error_type == "Conflict" else getattr(builtins, error_type, RuntimeError)
            raise error(response.get("message", "scripted failure"))
        return response


class _CycleLog:
    def __init__(self, events: list) -> None:
        self.events = events

    def _write(self, level: str, message: str, args: tuple) -> None:
        self.events.append({"kind": "log", "level": level, "message": message % args if args else message})

    def info(self, message: str, *args) -> None:
        self._write("info", message, args)

    def warning(self, message: str, *args) -> None:
        self._write("warning", message, args)


def _captured(value, api) -> object:
    if value is api:
        return "api"
    if isinstance(value, worker_loop.Claimed):
        return {key: _captured(item, api) for key, item in vars(value).items()}
    if isinstance(value, tuple):
        return [_captured(item, api) for item in value]
    if isinstance(value, list):
        return [_captured(item, api) for item in value]
    if isinstance(value, dict):
        return {key: _captured(item, api) for key, item in value.items()}
    return value


def capture_case(case: dict, entrypoint: str = "once", step: str = "dev") -> dict:
    events = []
    api = _ScriptedAPI(case["api"], events)
    worker = SimpleNamespace(
        account="claude-a", instance="claude-a.2", api=api, srv="/srv/aiflow", hb_s=30,
        run_as=["run-as"], control_url="https://control.invalid",
    )
    worker.publish = lambda **values: events.append({"kind": "publish", "values": values})
    patcher = pytest.MonkeyPatch()
    patcher.setattr(worker_loop.time, "time", lambda: 123.0)
    patcher.setattr(worker_loop, "log", _CycleLog(events))
    patcher.setattr(worker_loop, "attach_support_log", lambda result, srv: events.append(
        {"kind": "attach_support_log", "result": result, "srv": srv}))
    for runner_name in ("run_version_release", "run_ba_chat_turn", "run_verify", "run_post_merge", "run_triage",
                        "run_master", "run_intake", "run_impact", "run_dev"):
        def run(*args, _name=runner_name):
            events.append({"kind": "runner", "name": _name,
                           "args": [_captured(arg, api) for arg in args]})
            outcome = case["runners"].get(_name, {"outcome": "done"})
            if "raises" in outcome:
                error_type = getattr(builtins, outcome["raises"], RuntimeError)
                raise error_type(outcome.get("message", "runner failed"))
            return outcome
        patcher.setattr(worker_loop, runner_name, run)
    try:
        try:
            if entrypoint == "once":
                returned = worker_loop.Worker.once(worker)
            elif entrypoint == "_run_work_step":
                claim = worker_loop.Claimed(
                    run_id="run-direct", fence=3, run_token="direct-run-token", account=worker.account,
                    provider="codex", model="model-a", effort="high", ticket={"key": "SHOP-9"},
                    project={"key": "SHOP"}, identity=("node-a", "codex-a"), step=step,
                )
                returned = worker_loop._run_work_step(worker, claim, step)
            else:
                returned = getattr(worker_loop, entrypoint)(worker)
            result = {"return": returned}
        except Exception as exc:  # the version runner historically propagates its exception
            result = {"raises": type(exc).__name__, "message": str(exc)}
        result["events"] = events
        return result
    finally:
        patcher.undo()


def _expected() -> dict:
    path = Path(__file__).with_name("fixtures") / "worker_loop_cycle_parity.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    return {row["case"]: row["output"] for row in rows}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_once_matches_literal_pre_refactor_trace(case: dict) -> None:
    assert capture_case(case) == _expected()[case["name"]]


def test_version_release_phase_helper_runs_with_fake_api() -> None:
    output = capture_case(CASE_BY_NAME["version-success"], "_run_version_release_cycle")
    assert output["return"] is True
    assert [event["path"] for event in output["events"] if event["kind"] == "api"] == [
        "/v1/version-releases/candidates", "/v1/version-releases/claim",
        "/v1/runs/run-version/result", "/v1/runs/run-version/release",
    ]


def test_ba_chat_phase_helper_runs_with_fake_api() -> None:
    output = capture_case(CASE_BY_NAME["ba-chat-success"], "_run_ba_chat_cycle")
    assert output["return"] is True
    assert [event["path"] for event in output["events"] if event["kind"] == "api"] == [
        "/v1/ba-chat/candidate", "/v1/ba-chat/chat-3/result",
    ]


def test_work_phase_helper_runs_with_fake_api() -> None:
    output = capture_case(CASE_BY_NAME["work-dev-rich"], "_run_work_cycle")
    assert output["return"] is True
    assert any(event["kind"] == "runner" and event["name"] == "run_dev" for event in output["events"])


@pytest.mark.parametrize(("step", "runner"), [
    ("verify", "run_verify"), ("post_merge", "run_post_merge"), ("triage", "run_triage"),
    ("master", "run_master"), ("intake", "run_intake"), ("impact", "run_impact"),
    ("dev", "run_dev"), ("custom", "run_dev"),
])
def test_work_step_dispatch_helper_with_fake_runner(step: str, runner: str) -> None:
    case = {"name": "direct-step", "api": DEFAULT_API, "runners": {runner: {"outcome": "step"}}}
    output = capture_case(case, "_run_work_step", step)
    assert output["return"] == {"outcome": "step"}
    assert [event["name"] for event in output["events"] if event["kind"] == "runner"] == [runner]
