"""JAIF-341: silent developer runs, resumption, double-silence termination, signature tokens and Dashboard analytics."""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from fastapi import FastAPI

from aiflow.v4.adapters import RunResult, RunSpec
from aiflow.v4.control import analytics, app_helpers, login
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.incidents import flow_bug_candidates
from aiflow.v4.control.ui import build_console_page
from aiflow.v4.worker import dev
from aiflow.v4.worker.dev import Claimed, build_prompt, is_silent_response, run_dev_turns

UTC = timezone.utc


class FakeHeartbeat:
    """Mock heartbeat tracking lifecycle and progress."""

    def __init__(self) -> None:
        self.lost = threading.Event()
        self.stop = threading.Event()
        self.progress: dict[str, Any] = {}

    def start(self) -> None:
        pass

    def checkpoint(self) -> list[dict[str, Any]]:
        return []

    def include(self, _messages: list[dict[str, Any]]) -> None:
        pass


class MultiTurnAdapter:
    """Mock adapter returning sequential responses across turns."""

    def __init__(self, responses: list[RunResult]) -> None:
        self.responses = list(responses)
        self.specs: list[RunSpec] = []
        self.argv = lambda _spec: ["mock-adapter"]

    def run(self, spec: RunSpec, _on_progress: Any = None, stop: Any = None, on_start: Any = None) -> RunResult:
        self.specs.append(spec)
        if on_start:
            on_start()
        return self.responses.pop(0)


def sample_claimed(ticket_key: str = "TESTWP-341") -> Claimed:
    project = {"key": "TESTWP", "repo": "org/repo", "base": "develop", "checks": ["pytest tests/v4 -q"],
               "environment": [], "work_minutes": {"hard": 60}}
    ticket = {"key": ticket_key, "project": "TESTWP", "summary": "Fix silent runs",
              "description": "AC1: do not go silent", "labels": []}
    return Claimed(run_id="run-341", fence=1, run_token="token-341", account="claude-01",
                   provider="claude", model="test", effort="", ticket=ticket, project=project,
                   identity=("Bot", "bot@example.test"), step="dev")


def day_offset(back: int) -> datetime:
    today = datetime.now(UTC).date()
    return datetime.combine(today - timedelta(days=back), time(2, 0), tzinfo=UTC)


def minutes_ago(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def add_run_row(db, *, ticket: str, project: str = "TESTWP", outcome: str = "done",
                error_kind: str = "", summary: str = "", tokens_in: int = 0, tokens_out: int = 0,
                seconds: int | None = 600, started: datetime | None = None) -> str:
    run_id = str(uuid.uuid4())
    started = started or day_offset(0)
    ended = None if seconds is None else started + timedelta(seconds=seconds)
    db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
        "error_kind,summary,tokens_in,tokens_out,started_at,ended_at) "
        "VALUES (%s,%s,%s,'dev','claude-01','claude','node1',1,'finished',%s,%s,%s,%s,%s,%s,%s)",
        (run_id, ticket, project, outcome, error_kind, summary, tokens_in, tokens_out, started, ended),
    )
    return run_id


def test_developer_prompt_forbids_promises_to_report_later_and_requires_waiting_for_commands():
    prompt = build_prompt(sample_claimed(), "TESTWP-341-branch")
    assert "never end a turn with a promise to report later" in prompt.lower()
    assert "wait for every command to finish and report only the finished result" in prompt.lower()
    assert "start long commands (including test suites) in the foreground" in prompt.lower()
    assert "run's own time slice" in prompt.lower()


def test_silent_response_detector_identifies_idle_and_unfulfilled_promises():
    assert is_silent_response(RunResult(error_kind="idle")) is True
    assert is_silent_response(RunResult(final_text="", error_kind="")) is True
    assert is_silent_response(RunResult(final_text="I will report when the tests finish.")) is True
    assert is_silent_response(RunResult(final_text="I'll report once the build completes.")) is True
    assert is_silent_response(RunResult(final_text="I'll report when the tests finish")) is True
    assert is_silent_response(RunResult(final_text="I will update the README")) is False
    assert is_silent_response(RunResult(final_text="SUMMARY: Done\nRESULT: DONE", verdict="DONE")) is False
    assert is_silent_response(RunResult(final_text="SUMMARY: Missing tool\nRESULT: BLOCKED", verdict="BLOCKED")) is False
    assert is_silent_response(RunResult(error_kind="quota")) is False
    assert is_silent_response(RunResult(error_kind="timeout")) is False


def test_failed_verdict_whose_reason_mentions_silent_is_not_counted_as_silent(monkeypatch, tmp_path):
    class MockGit:
        def __init__(self, _root, run_as=None) -> None:
            pass

        def mirror(self, *_args) -> str:
            return "mirror"

        def foreign_worktree(self, *_args) -> None:
            return None

        def worktree(self, _mirror, _wt, _branch, base) -> str:
            return f"origin/{base}"

        def run(self, *_args, **_kwargs) -> str:
            return "checkout-head"

        def changed(self, _wt) -> list[str]:
            return ["file.py"]

    class MockAPI:
        node_name = "test-node"

        def call(self, method: str, path: str, body: Any = None, params: Any = None) -> dict[str, Any]:
            if path == "/v1/github/token":
                return {"token": "git-token"}
            return {"ok": True}

    adapter = MultiTurnAdapter([
        RunResult(final_text="SUMMARY: Cache not cleared\nRESULT: FAIL", verdict="FAIL",
                  verdict_reason="the silent cache was never invalidated", tokens_in=1000, duration_s=5),
    ])

    monkeypatch.setattr(dev, "project_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(dev, "Git", MockGit)
    monkeypatch.setattr(dev, "Heartbeat", lambda _api, _claim, _every: FakeHeartbeat())
    monkeypatch.setattr(dev, "get_adapter", lambda _provider: adapter)

    result = dev.run_dev(MockAPI(), sample_claimed(), str(tmp_path), "https://control.example.test", 30, [])

    assert len(adapter.specs) == 1
    assert result.get("error_kind") != "silent"


def test_silent_run_is_resumed_once_with_continuation_instruction():
    adapter = MultiTurnAdapter([
        RunResult(final_text="I will report when the background test finishes.", tokens_in=5000, duration_s=10),
        RunResult(final_text="SUMMARY: Finished the task\nTESTS: all passed\nRESULT: DONE", verdict="DONE",
                  tokens_in=3000, tokens_out=1200, duration_s=15),
    ])
    heartbeat = FakeHeartbeat()
    spec = RunSpec(prompt="Implement ticket TESTWP-341", cwd="/tmp/repo", account_home="/tmp/account", timeout_s=1800)

    aggregate = run_dev_turns(adapter, spec, heartbeat)

    assert len(adapter.specs) == 2
    second_prompt = adapter.specs[1].prompt
    assert "Continue the same AI Flow run and report your result" in second_prompt
    assert "Your previous turn went silent without reporting a finished result" in second_prompt
    assert "Do not leave commands running in the background" in second_prompt
    assert "Implement ticket TESTWP-341" in second_prompt
    assert adapter.specs[1].log_append is True
    assert aggregate.verdict == "DONE"
    assert aggregate.error_kind == ""
    assert aggregate.tokens_in == 8000
    assert aggregate.tokens_out == 1200
    assert aggregate.duration_s == 25


def test_resumed_run_that_goes_silent_again_ends_with_clear_reason_that_it_went_silent_twice():
    adapter = MultiTurnAdapter([
        RunResult(final_text="I will report after the test suite completes.", tokens_in=4000, duration_s=12),
        RunResult(error_kind="idle", tokens_in=2000, duration_s=20),
    ])
    heartbeat = FakeHeartbeat()
    spec = RunSpec(prompt="Implement ticket TESTWP-341", cwd="/tmp/repo", account_home="/tmp/account", timeout_s=1800)

    aggregate = run_dev_turns(adapter, spec, heartbeat)

    assert len(adapter.specs) == 2
    assert aggregate.error_kind == "silent"
    assert aggregate.verdict == ""
    assert aggregate.verdict_reason == "the run went silent twice"
    assert aggregate.tokens_in == 6000
    assert aggregate.duration_s == 32


def test_dev_worker_publishes_failure_with_clear_reason_when_run_goes_silent_twice(monkeypatch, tmp_path):
    events: list[str] = []

    class MockGit:
        def __init__(self, _root, run_as=None) -> None:
            pass

        def mirror(self, *_args) -> str:
            return "mirror"

        def foreign_worktree(self, *_args) -> None:
            return None

        def worktree(self, _mirror, _wt, _branch, base) -> str:
            return f"origin/{base}"

        def run(self, *_args, **_kwargs) -> str:
            return "checkout-head"

        def changed(self, _wt) -> list[str]:
            return ["file.py"]

    class MockAPI:
        node_name = "test-node"

        def call(self, method: str, path: str, body: Any = None, params: Any = None) -> dict[str, Any]:
            events.append(f"{method} {path}")
            if path == "/v1/github/token":
                return {"token": "git-token"}
            return {"ok": True}

    adapter = MultiTurnAdapter([
        RunResult(final_text="I will report later.", tokens_in=1000, duration_s=5),
        RunResult(final_text="Still running in background, will report when it finishes.", tokens_in=1500, duration_s=8),
    ])

    monkeypatch.setattr(dev, "project_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(dev, "Git", MockGit)
    monkeypatch.setattr(dev, "Heartbeat", lambda _api, _claim, _every: FakeHeartbeat())
    monkeypatch.setattr(dev, "get_adapter", lambda _provider: adapter)

    result = dev.run_dev(MockAPI(), sample_claimed(), str(tmp_path), "https://control.example.test", 30, [])

    assert result["outcome"] == "failed"
    assert result["error_kind"] == "silent"
    assert result["summary"] == "the run went silent twice"
    assert any("comment" in call for call in events)
    assert any("transition" in call for call in events)


def test_silent_run_counts_once_toward_signature_detector_and_shows_tokens(ctx):
    maintenance_project = ctx.settings.maintenance_project
    # The detector uses a rolling six-hour window, so these runs are recent whatever the time of day.
    recent = minutes_ago(15)
    run_id1 = add_run_row(ctx.db, ticket=f"{maintenance_project}-1", outcome="failed", error_kind="silent",
                          summary="the run went silent twice", tokens_in=40000, tokens_out=5000, project=maintenance_project,
                          started=recent)
    run_id2 = add_run_row(ctx.db, ticket=f"{maintenance_project}-1", outcome="failed", error_kind="silent",
                          summary="the run went silent twice", tokens_in=30000, tokens_out=4000, project=maintenance_project,
                          started=recent)
    run_id3 = add_run_row(ctx.db, ticket=f"{maintenance_project}-2", outcome="failed", error_kind="silent",
                          summary="the run went silent twice", tokens_in=50000, tokens_out=6000, project=maintenance_project,
                          started=recent)

    candidates = flow_bug_candidates(ctx)
    silent_candidates = [c for c in candidates if "silent" in c["pattern"] or c["error_kind"] == "silent"]
    assert len(silent_candidates) == 1
    candidate = silent_candidates[0]

    assert len(candidate["runs"]) == 3
    run_ids = {r["id"] for r in candidate["runs"]}
    assert run_ids == {run_id1, run_id2, run_id3}

    assert candidate["tokens_used"] == 135000
    tokens_by_id = {r["id"]: r["tokens"] for r in candidate["runs"]}
    assert tokens_by_id[run_id1] == 45000
    assert tokens_by_id[run_id2] == 34000
    assert tokens_by_id[run_id3] == 56000

    created_issues: list[dict[str, Any]] = []

    class MockJira:
        def create_issue(self, project: str, summary: str, description: str, issuetype: str = "Bug",
                         labels: list[str] | None = None, priority: str = "") -> str:
            created_issues.append({"project": project, "summary": summary, "description": description,
                                   "priority": priority})
            return f"{project}-999"

        def status(self, _key: str) -> str:
            return "Intake"

        def transition_to(self, _key: str, target: str) -> str:
            return target

    ctx.jira = MockJira()
    helpers = app_helpers.install(FastAPI(), ctx, SimpleNamespace(late=SimpleNamespace()))
    reports = helpers.process_flow_bugs({"id": run_id1, "fence": 1, "account_id": "claude-01", "node": "node1"})
    assert len(reports) == 1
    assert reports[0]["action"] == "created"
    assert len(created_issues) == 1
    issue_desc = created_issues[0]["description"]
    assert "Tokens wasted: 135,000" in issue_desc
    assert "45,000 tokens" in issue_desc
    assert created_issues[0]["priority"] == "Highest"


def test_analytics_silent_runs_calculates_daily_tickets_and_two_percent_target(db):
    with pytest.raises(ValueError):
        analytics.silent_runs(db, 0)
    with pytest.raises(ValueError):
        analytics.silent_runs(db, 95)
    with pytest.raises(ValueError):
        analytics.silent_runs(db, True)  # type: ignore[arg-type]

    empty_report = analytics.silent_runs(db, 30)
    assert empty_report["days"] == 30
    assert (empty_report["developer_runs"], empty_report["silent_runs"],
            empty_report["silent_share_pct"], empty_report["target_share_pct"]) == (0, 0, 0.0, 2.0)
    assert empty_report["daily"] == []
    assert empty_report["tickets"] == []

    add_run_row(db, ticket="TESTWP-10", outcome="failed", error_kind="silent")
    add_run_row(db, ticket="TESTWP-10", outcome="failed", error_kind="idle")
    add_run_row(db, ticket="TESTWP-11", outcome="done")
    add_run_row(db, ticket="OTHER-10", project="OTHER", outcome="failed", error_kind="silent")

    report = analytics.silent_runs(db, 1)
    assert (report["developer_runs"], report["silent_runs"], report["target_share_pct"]) == (4, 3, 2.0)
    assert report["silent_share_pct"] == 75.0
    assert report["daily"][0]["day"] == day_offset(0).date().isoformat()
    assert (report["daily"][0]["developer_runs"], report["daily"][0]["silent_runs"]) == (4, 3)
    tickets = {row["ticket"]: row for row in report["tickets"]}
    assert set(tickets) == {"TESTWP-10", "OTHER-10"}
    assert (tickets["TESTWP-10"]["developer_runs"], tickets["TESTWP-10"]["silent_runs"]) == (2, 2)


def test_dashboard_silent_runs_endpoint_serves_scoped_analytics_and_enforces_permissions(client, ctx):
    add_run_row(ctx.db, ticket="TESTWP-20", outcome="failed", error_kind="silent")
    add_run_row(ctx.db, ticket="OTHER-20", project="OTHER", outcome="failed", error_kind="silent")

    response = client.get("/v1/dashboard/silent-runs", params={"days": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["silent_runs"] == 2
    assert data["target_share_pct"] == 2.0

    ctx.settings.console_login = True
    ctx.settings.console_session_secret = "test-dashboard-silent-secret-with-at-least-32-bytes"
    assert client.get("/v1/dashboard/silent-runs").status_code == 401

    login.seed_user(ctx.db, "silent-limited@example.test", "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["TESTWP"], "silent-limited@example.test"))
    client.cookies.set(COOKIE_NAME, issue_session("silent-limited@example.test", "business", 2_000_000_000,
                                                  ctx.settings.console_session_secret))
    scoped = client.get("/v1/dashboard/silent-runs").json()
    assert {row["project"] for row in scoped["tickets"]} == {"TESTWP"}
    assert scoped["silent_runs"] == 1 and scoped["developer_runs"] == 1


def test_dashboard_console_page_injects_silent_runs_panel_with_two_percent_target():
    page = build_console_page()
    assert "function dashboardSilentRunsPanel()" in page
    assert 'data-block="silent-runs"' in page
    assert "Silent developer runs" in page
    assert "Silent share" in page
    assert "Target below 2%" in page
    assert "fetch('/v1/dashboard/silent-runs?days=30'" in page
    assert "loadDashboardSilentRuns(false)" in page
    assert "share < 2 ? 'good' : 'warn'" in page
