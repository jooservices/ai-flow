from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from aiflow.v4.adapters import RunResult
from aiflow.v4.worker import dev, verify


class RecordingAPI:
    node_name = "test-node"

    def __init__(self, events):
        self.events = events

    def call(self, method, path, body=None, params=None):
        self.events.append(f"api:{method} {path}")
        if path == "/v1/github/token":
            return {"token": "test-token"}
        if method == "GET" and path == "/v1/github/pr":
            return {"found": False}
        if path == "/v1/github/pr":
            return {"url": "https://github.example.test/pr/1"}
        return {"ok": True}


class RecordingHeartbeat:
    def __init__(self, _api, _claim, _every, events):
        self.events = events
        self.lost = threading.Event()
        self.stop = RecordingStop(events)
        self.progress = {}

    def start(self):
        self.events.append("heartbeat:start")

    def checkpoint(self):
        self.events.append("heartbeat:checkpoint")
        return []


class RecordingStop:
    def __init__(self, events):
        self.events = events
        self.event = threading.Event()

    def set(self):
        self.events.append("heartbeat:stop")
        self.event.set()


class RecordingGit:
    def __init__(self, _root, events, changed):
        self.events = events
        self.changed_files = changed
        self.events.append("git:init")

    def mirror(self, *_args):
        self.events.append("git:mirror")
        return "mirror"

    def foreign_worktree(self, *_args):
        self.events.append("git:foreign_worktree")
        return None

    def worktree(self, _mirror, _wt, _branch, base):
        self.events.append("git:worktree")
        return f"origin/{base}"

    def changed(self, _wt):
        self.events.append("git:changed")
        return self.changed_files

    def commit_all(self, _wt, _message, _identity):
        self.events.append("git:commit_all")
        return "a" * 40

    def push(self, _wt, _branch, _token):
        self.events.append("git:push")


class RecordingAdapter:
    argv = staticmethod(lambda _spec: [])

    def __init__(self, events, result):
        self.events = events
        self.result = result

    def run(self, _spec, _on_progress=None, stop=None, on_start=None):
        self.events.append("adapter:run")
        if on_start:
            on_start()
        return self.result


def run_case(monkeypatch, tmp_path, result, changed):
    events = []
    api = RecordingAPI(events)
    project = {"key": "JAIF", "repo": "org/repo", "base": "develop", "checks": [],
               "environment": [], "work_minutes": {"hard": 600}}
    ticket = {"key": "JAIF-312", "project": "JAIF", "summary": "Split dev run",
              "description": "", "labels": []}
    claim = SimpleNamespace(run_id="run-1", fence=7, run_token="run-token", account="claude-1",
                            provider="claude", model="test", effort="", ticket=ticket, project=project,
                            identity=("Worker", "worker@example.test"), step="dev", findings="", branch="", related=[],
                            context_pack={}, machine_tools={}, triage_answer="", triage_question="")
    monkeypatch.setattr(dev, "project_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(dev, "Git", lambda root, run_as=None: RecordingGit(root, events, changed))
    monkeypatch.setattr(dev, "Heartbeat", lambda api, claim, every: RecordingHeartbeat(api, claim, every, events))
    monkeypatch.setattr(dev, "get_adapter", lambda _provider: RecordingAdapter(events, result))
    return dev.run_dev(api, claim, str(tmp_path), "https://control.example.test", 30, []), events


@pytest.mark.parametrize(
    ("result", "changed", "expected_outcome", "tail"),
    [
        (RunResult(final_text="SUMMARY: Implemented it\nTESTS: pytest -q", duration_s=1),
         ["src/change.py"], "done", ["git:changed", "git:commit_all", "api:POST /v1/github/token",
                                     "git:push", "api:POST /v1/github/pr",
                                     "api:POST /v1/jira/JAIF-312/comment",
                                     "api:POST /v1/jira/JAIF-312/transition", "heartbeat:stop"]),
        (RunResult(final_text="SUMMARY: Provider failed", error_kind="provider", duration_s=1),
         ["src/change.py"], "failed", ["git:changed", "api:POST /v1/jira/JAIF-312/comment",
                                        "api:POST /v1/jira/JAIF-312/transition", "heartbeat:stop"]),
        (RunResult(final_text="SUMMARY: Tests failed", verdict="FAIL", verdict_reason="tests failed", duration_s=1),
         ["src/change.py"], "failed", ["git:changed", "api:POST /v1/jira/JAIF-312/comment",
                                        "api:POST /v1/jira/JAIF-312/transition", "heartbeat:stop"]),
        (RunResult(final_text="SUMMARY: Nothing needed", duration_s=1), [], "failed",
         ["git:changed", "api:GET /v1/github/pr", "api:POST /v1/jira/JAIF-312/comment",
          "api:POST /v1/jira/JAIF-312/transition", "heartbeat:stop"]),
    ],
    ids=["success", "failed-ai", "failed-tests", "no-change"],
)
def test_dev_run_side_effect_order_is_characterized(monkeypatch, tmp_path, result, changed, expected_outcome, tail):
    outcome, events = run_case(monkeypatch, tmp_path, result, changed)

    assert outcome["outcome"] == expected_outcome
    setup = ["git:init", "heartbeat:start", "api:POST /v1/github/token", "git:mirror", "git:foreign_worktree",
             "git:worktree", "api:POST /v1/jira/JAIF-312/transition", "api:POST /v1/jira/JAIF-312/fields",
             "api:POST /v1/jira/JAIF-312/comment"]
    provider = ["adapter:run"] + ([] if result.error_kind else ["heartbeat:checkpoint"])
    assert events == setup + provider + tail


def test_verify_uses_the_shared_fenced_setup_and_reporting(monkeypatch, tmp_path):
    events = []

    class VerifyGit:
        def __init__(self, _root, run_as=None):
            events.append("git:init")

        def mirror(self, *_args):
            events.append("git:mirror")
            return "mirror"

        def remote_branch_exists(self, *_args):
            return True

        def detached(self, *_args):
            events.append("git:detached")
            return "abc123456789"

        def merge_base_in(self, *_args, **_kwargs):
            return []

        def behind(self, *_args):
            return False

        def scrub(self, _worktree):
            events.append("git:scrub")

    api = RecordingAPI(events)
    project = {"key": "JAIF", "repo": "org/repo", "base": "develop", "checks": [],
               "environment": [], "work_minutes": {"hard": 600}, "merge": {"auto": False}}
    ticket = {"key": "JAIF-312", "project": "JAIF", "summary": "Split dev run", "description": "", "labels": []}
    claim = SimpleNamespace(run_id="run-1", fence=7, run_token="run-token", account="claude-1",
                            provider="claude", model="test", effort="", ticket=ticket, project=project,
                            identity=("Worker", "worker@example.test"), step="verify", related=[], context_pack={})
    adapter = RecordingAdapter(events, RunResult(final_text="VERDICT: PASS\nCHECKS: unit", duration_s=1))
    monkeypatch.setattr(verify, "Git", VerifyGit)
    monkeypatch.setattr(verify, "Heartbeat", lambda api, claim, every: RecordingHeartbeat(api, claim, every, events))
    monkeypatch.setattr(verify, "project_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(verify, "get_adapter", lambda _provider: adapter)
    monkeypatch.setattr(verify, "build_prompt", lambda *_args: "review")
    monkeypatch.setattr(verify, "wait_for_ci", lambda *_args: [])

    result = verify.run_verify(api, claim, {"number": 1, "branch": "JAIF-312-split", "url": "pr-url"},
                               str(tmp_path), "https://control.example.test", 30, [])

    assert result["outcome"] == "human"
    assert events[:4] == ["git:init", "heartbeat:start", "api:POST /v1/github/token", "git:mirror"]
    assert events[-3:] == ["api:POST /v1/jira/JAIF-312/transition", "heartbeat:stop", "git:scrub"]
    assert "api:POST /v1/github/comment" in events


def test_deferred_check_mapping_helpers_keep_only_configured_checks():
    criteria = [{"id": "AC1", "check": "pytest"}, {"id": "AC2", "check": "missing"}, {"id": "AC3"}]

    unmapped = verify.unmapped_deferred(criteria, {"pytest"})

    assert [item["id"] for item in unmapped] == ["AC2", "AC3"]
    assert verify.deferred_ids(unmapped) == "AC2, AC3"
