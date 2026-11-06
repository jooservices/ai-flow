from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from aiflow.v4.adapters import RunResult
from aiflow.v4.worker import impact, run_steps


def test_shared_workspace_and_environment_resolution_keep_the_existing_values():
    project = {"environment": [{"name": "WP_BASE_URL"}]}
    calls = []

    def environment_factory(project_key, items, phase):
        calls.append((project_key, items, phase))
        return {"WP_BASE_URL": "https://example.test"}

    assert run_steps.worktree_path("/srv/aiflow", "SHOP", "SHOP-4", "--impact") == Path(
        "/srv/aiflow/work/SHOP/SHOP-4--impact")
    assert run_steps.project_environment("SHOP", project, "verify", environment_factory) == {
        "WP_BASE_URL": "https://example.test",
    }
    assert calls == [("SHOP", project["environment"], "verify")]


def test_project_environment_keeps_configured_values_and_run_overrides(monkeypatch):
    from aiflow.v4.core import settings

    monkeypatch.setattr(settings, "read_env_file", lambda _path: {"WP_PASSWORD": "configured-secret"})
    env = run_steps.project_env("SHOP", [
        {"name": "WP_BASE_URL", "kind": "base_url", "value": "https://example.test"},
        {"name": "WP_PASSWORD", "kind": "secret", "runs": ["dev"]},
    ], "dev", {"WP_BASE_URL": "https://temporary.test"})

    assert env["WP_BASE_URL"] == "https://temporary.test"
    assert env["WP_PASSWORD"] == "configured-secret"


def test_run_spec_preserves_the_hard_budget_and_accepts_the_impact_target_budget(tmp_path):
    claim = SimpleNamespace(account="worker-1", model="sonnet", effort="high", run_token="token")
    project = {"work_minutes": {"hard": 25, "target": 10}}
    base = ("prompt", tmp_path / "work", str(tmp_path), claim, project, "https://control.test",
            tmp_path / "run.jsonl", {"WP_BASE_URL": "https://example.test"})

    hard = run_steps.make_run_spec(*base)
    target = run_steps.make_run_spec(*base, timeout_s=600)

    assert (hard.timeout_s, target.timeout_s) == (1500, 600)
    assert hard.extra_env == target.extra_env == {"WP_BASE_URL": "https://example.test"}
    assert hard.account_home == str(tmp_path / "accounts" / "worker-1")


def test_set_run_progress_keeps_the_assigned_payload_available_for_extension():
    heartbeat = SimpleNamespace(progress={})

    progress = run_steps.set_run_progress(heartbeat, "worktree", start="origin/develop", conflicts=0)

    assert progress is heartbeat.progress
    progress["repo_change"] = "old -> new"
    assert heartbeat.progress == {"phase": "worktree", "start": "origin/develop", "conflicts": 0,
                                  "repo_change": "old -> new"}


def test_run_notes_keep_the_existing_text_and_hide_environment_values(monkeypatch):
    claim = SimpleNamespace(step="dev", account="worker-1", provider="claude", model="sonnet",
                            run_id="run-12345678", fence=9)
    monkeypatch.setattr(run_steps.time, "time", lambda: 130)

    message = run_steps.note(claim, "started", what="  Work began  ", t0=100, node="node-4")
    environment = run_steps.environment_notes(
        [{"name": "WP_PASSWORD", "kind": "secret", "runs": ["dev"]}],
        "dev",
        {"WP_PASSWORD": "do-not-print"},
    )

    assert message == ("AI Flow · dev · started\nWhat: Work began\n"
                      "Run: worker-1 (claude/sonnet) · 0 min 30 s · run run-1234 · fence 9")
    assert "WP_PASSWORD (secret, set)" in environment
    assert "do-not-print" not in environment
    assert run_steps.environment_status(
        [{"name": "WP_PASSWORD", "kind": "secret", "runs": ["dev"]}], "dev", {}) == (
            "WP_PASSWORD (not provided)")


def test_developer_and_verifier_progress_keep_their_phase_payloads(monkeypatch):
    heartbeat = SimpleNamespace(progress={})
    monkeypatch.setattr(run_steps.time, "time", lambda: 110)
    developer = run_steps.RunProgress(heartbeat, 100, "ai", include_last_event=True)

    developer({"type": "assistant_message"})

    assert heartbeat.progress == {"phase": "ai", "events": 1, "last": "assistant_message", "elapsed_s": 10}

    monkeypatch.setattr(run_steps.time, "time", lambda: 125)
    verifier = run_steps.RunProgress(heartbeat, 100, "verify-ai")

    verifier({"type": "tool_use"})

    assert heartbeat.progress == {"phase": "verify-ai", "events": 1, "elapsed_s": 25}


def test_run_result_metadata_keeps_provider_values_and_reset_format():
    response = RunResult(tokens_in=11, tokens_out=7, cost_usd=0.25, quota={"remaining": 4},
                         quota_observed_at=datetime(2026, 1, 2, tzinfo=UTC), error_kind="",
                         reset_at=datetime(2026, 1, 3, tzinfo=UTC))

    assert run_steps.run_result_metadata(response) == {
        "tokens_in": 11, "tokens_out": 7, "cost_usd": 0.25, "quota": {"remaining": 4},
        "quota_observed_at": "2026-01-02T00:00:00+00:00", "error_kind": "",
        "reset_at": "2026-01-03T00:00:00+00:00",
    }


class _RecordingAPI:
    node_name = "test-node"

    def __init__(self, events):
        self.events = events

    def call(self, method, path, body=None, params=None):
        self.events.append(f"api:{method} {path}")
        if path == "/v1/github/token":
            return {"token": "test-token"}
        return {"ok": True}


class _RecordingHeartbeat:
    def __init__(self, _api, _claim, _every, events):
        self.events = events
        self.lost = threading.Event()
        self.stop = SimpleNamespace(set=lambda: events.append("heartbeat:stop"))
        self.progress = {}

    def start(self):
        self.events.append("heartbeat:start")

    def checkpoint(self):
        self.events.append("heartbeat:checkpoint")
        return []


class _RecordingGit:
    def __init__(self, _root, run_as=None):
        self.events = _events
        self.events.append("git:init")

    def mirror(self, *_args):
        self.events.append("git:mirror")
        return "mirror"

    def detached(self, *_args):
        self.events.append("git:detached")
        return "abc123456789"


_events = []


class _RecordingAdapter:
    argv = staticmethod(lambda _spec: [])

    def run(self, _spec, _on_progress=None, stop=None, on_start=None):
        _events.append("adapter:run")
        return RunResult(final_text=("IMPACT: DONE\nSIZE: M\nEPICS: SHOP-1\nTICKETS: none\n"
                                    "RISKS:\n- none\nCONFLICTS:\n- none"),
                         tokens_in=11, tokens_out=7, cost_usd=0.25, quota={"remaining": 4},
                         quota_observed_at=datetime(2026, 1, 2, tzinfo=UTC), error_kind="",
                         reset_at=datetime(2026, 1, 3, tzinfo=UTC), exit_code=0, duration_s=2,
                         log_path="run.jsonl")


def test_impact_workspace_run_notes_and_result_are_characterized(monkeypatch, tmp_path):
    _events.clear()
    api = _RecordingAPI(_events)
    claim = SimpleNamespace(run_id="run-1", fence=7, run_token="token", account="worker-1", provider="claude",
                            model="sonnet", effort="", ticket={"key": "SHOP-4", "project": "SHOP"},
                            project={"key": "SHOP", "repo": "org/shop", "base": "develop"}, step="impact",
                            project_tickets=[], context_pack={})
    adapter = _RecordingAdapter()
    monkeypatch.setattr(impact, "Git", _RecordingGit)
    monkeypatch.setattr(impact, "Heartbeat",
                        lambda api, claim, every: _RecordingHeartbeat(api, claim, every, _events))
    monkeypatch.setattr(impact, "get_adapter", lambda _provider: adapter)
    monkeypatch.setattr(impact, "read_spec", lambda _worktree: "project spec")
    monkeypatch.setattr(impact, "build_prompt", lambda _claim, _spec: "impact prompt")

    result = impact.run_impact(api, claim, str(tmp_path), "https://control.test", 30, [])

    assert result == {
        "fence": 7, "outcome": "assessed", "tokens_in": 11, "tokens_out": 7, "cost_usd": 0.25,
        "quota": {"remaining": 4}, "quota_observed_at": "2026-01-02T00:00:00+00:00", "error_kind": "",
        "reset_at": "2026-01-03T00:00:00+00:00",
        "details": {"exit_code": 0, "duration_s": 2, "log": "run.jsonl",
                    "impact": {"size": "M", "epics": ["SHOP-1"], "tickets": [], "risks": [], "conflicts": []}},
        "summary": "impact M: 1 epics, 0 tickets, 0 conflicts",
    }
    assert _events == ["git:init", "heartbeat:start", "api:POST /v1/github/token", "git:mirror",
                       "git:detached", "adapter:run", "heartbeat:checkpoint", "api:POST /v1/jira/SHOP-4/comment",
                       "heartbeat:stop"]
