"""Developer run budgets, timeout recovery and the full-suite hand-off."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.core.work_limits import retry_hard_minutes
from aiflow.v4.worker import dev
from aiflow.v4.worker.dev import Claimed, build_prompt


def _claimed(project: dict | None = None) -> Claimed:
    return Claimed(run_id="run-timeout-1", fence=3, run_token="run-token", account="claude-01",
                   provider="claude", model="opus", effort="high",
                   ticket={"key": "TESTWP-800", "project": "TESTWP", "summary": "Add profile editing",
                           "description": "Acceptance criteria:\n- AC1: A profile can be edited.",
                           "labels": [], "priority": "Medium", "type": "Story"},
                   project=project or {"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "base": "develop",
                                       "checks": ["vendor/bin/phpunit"], "work_minutes": {"hard": 90},
                                       "timeout_retries": 1}, identity=("AI Flow", "bot@example.test"))


def test_developer_prompt_orders_focused_checks_before_one_full_suite_and_reserves_its_slice():
    prompt = build_prompt(_claimed(), "TESTWP-800-profile-editing")

    assert "checks that cover the changed code first" in prompt
    assert "full configured suite exactly once at the end" in prompt
    assert "Reserve the final 15-minute test time slice" in prompt
    assert "FULL SUITE: FINISHED" in prompt and "FULL SUITE: UNFINISHED" in prompt
    assert "timed out 1 time(s)" in prompt and "hard limit to 90 minutes" in prompt


def test_full_suite_status_reports_missing_or_unfinished_results():
    assert dev._suite_status("FULL SUITE: FINISHED", False) == {"status": "finished", "reason": ""}
    assert dev._suite_status("FULL SUITE: UNFINISHED - the time slice ended", False) == {
        "status": "unfinished", "reason": "the time slice ended"}
    assert dev._suite_status("FULL SUITE: FINISHED", True) == {
        "status": "unfinished", "reason": "developer run reached its hard time limit"}
    assert dev._suite_status("SUMMARY: change made", False)["status"] == "unreported"


def test_hard_timeout_preserves_changes_and_hands_full_suite_to_pull_request_checks(tmp_path: Path):
    class Api:
        node_name = "cc"

        def __init__(self):
            self.calls = []

        def call(self, method, path, body=None, params=None):
            self.calls.append((method, path, body or {}))
            if path == "/v1/github/token":
                return {"token": "synthetic-test-credential"}
            if path == "/v1/github/pr":
                return {"url": "https://github.example.test/pull/800", "number": 800}
            return {}

    class Git:
        def __init__(self):
            self.pushed = []

        def changed(self, _worktree):
            return ["src/profile.py", "tests/test_profile.py"]

        def commit_all(self, _worktree, _message, _identity):
            return "abc123"

        def push(self, _worktree, branch, token):
            self.pushed.append((branch, token))

    api, git, claimed = Api(), Git(), _claimed()
    result = {"details": {}}
    handed = {"ac_map": "- AC1: src/profile.py | tests/test_profile.py"}
    done = dev._handover_timed_out(api, claimed, git, tmp_path, "TESTWP-800-profile-editing",
                                   claimed.project["repo"], "Profile editor implemented", handed, result, 10.0)

    assert done is True and result["outcome"] == "done" and result["error_kind"] == "timeout"
    assert result["pr_url"] == "https://github.example.test/pull/800" and result["head_sha"] == "abc123"
    assert result["details"]["full_suite"] == {"status": "unfinished",
                                                "reason": "developer run reached its hard time limit"}
    assert result["details"]["handed_to_pipeline"] is True
    assert git.pushed == [("TESTWP-800-profile-editing", "synthetic-test-credential")]
    pr = next(body for method, path, body in api.calls if method == "POST" and path == "/v1/github/pr")
    assert pr["draft"] is True and "pipeline must complete the configured checks" in pr["body"]
    assert "Acceptance criteria map" in pr["body"]
    assert [body["to"] for method, path, body in api.calls if path.endswith("/transition")] == ["In Verify"]
    comment = next(body["body"] for _method, path, body in api.calls if path.endswith("/comment"))
    assert "full suite handed to pipeline" in comment and "pipeline completes" in comment
    assert "synthetic-test-credential" not in pr["body"]


def test_explicitly_unfinished_suite_is_reported_in_pull_request_handover(monkeypatch, tmp_path: Path):
    class Api:
        node_name = "cc"

        def __init__(self):
            self.calls = []

        def call(self, method, path, body=None, params=None):
            self.calls.append((method, path, body or {}))
            if path == "/v1/github/token":
                return {"token": "synthetic-test-credential"}
            if path == "/v1/github/pr":
                return {"url": "https://github.example.test/pull/801", "number": 801}
            return {}

    class Git:
        def __init__(self, *_args, **_kwargs):
            pass

        def mirror(self, *_args):
            return "mirror"

        def foreign_worktree(self, *_args):
            return None

        def worktree(self, _mirror, _path, _branch, base):
            return "origin/" + base

        def changed(self, _worktree):
            return ["src/profile.py"]

        def commit_all(self, _worktree, _message, _identity):
            return "abc801"

        def push(self, *_args):
            return None

    class Adapter:
        argv = lambda self, _spec: []  # noqa: E731

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0, quota={}, error_kind="", reset_at=None,
                                   verdict="DONE", verdict_reason="", error_message="",
                                   final_text=("SUMMARY: Profile editing implemented.\n"
                                               "TESTS: focused profile tests passed.\n"
                                               "FULL SUITE: UNFINISHED - reserved test slice expired"),
                                   permission_denials=[], exit_code=0, duration_s=10, log_path="")

    monkeypatch.setattr(dev, "Git", Git)
    monkeypatch.setattr(dev, "get_adapter", lambda _provider: Adapter())
    api, claimed = Api(), _claimed()
    result = dev.run_dev(api, claimed, str(tmp_path), "https://control", 3600, [])

    assert result["outcome"] == "done" and result["error_kind"] == ""
    assert result["details"]["full_suite"] == {"status": "unfinished", "reason": "reserved test slice expired"}
    assert result["details"]["handed_to_pipeline"] is True
    pr = next(body for method, path, body in api.calls if method == "POST" and path == "/v1/github/pr")
    assert "full suite unfinished: reserved test slice expired" in pr["body"]
    assert "pipeline must complete the configured checks before merge" in pr["body"]
    assert [body["to"] for method, path, body in api.calls if path.endswith("/transition")] == [
        "In Progress", "In Verify"]


def test_timeout_retries_receive_a_larger_limit_and_record_the_increase(client, ctx):
    joined = join_node(client, "cc")
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    add_ticket(ctx.db, "TESTWP-800")
    started = datetime.now(timezone.utc) - timedelta(minutes=100)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,"
               "started_at,ended_at) VALUES (%s,'TESTWP-800','TESTWP','dev','claude-01','claude','cc',1,'finished',"
               "'timeout','timeout',%s,%s)", (str(uuid.uuid4()), started, started + timedelta(minutes=60)))

    first = client.post("/v1/work/claim", json={"ticket": "TESTWP-800", "account": "claude-01"}, headers=headers)
    assert first.status_code == 200, first.text
    claimed = first.json()
    assert claimed["project"]["work_minutes"]["hard"] == 90
    assert claimed["project"]["timeout_retries"] == 1
    run = ctx.db.one("SELECT details FROM runs WHERE id=%s", (claimed["run_id"],))
    assert run["details"]["timeout_retry"] == {"previous_timeouts": 1, "hard_minutes": 90}

    reported = client.post(f"/v1/runs/{claimed['run_id']}/result", json={
        "fence": claimed["fence"], "outcome": "timeout", "error_kind": "timeout"}, headers=headers)
    assert reported.status_code == 200
    client.post(f"/v1/runs/{claimed['run_id']}/release", json={"fence": claimed["fence"]}, headers=headers)
    ctx.db.run("UPDATE runs SET started_at=now()-interval '2 hours',ended_at=now()-interval '1 hour' "
               "WHERE ticket_key='TESTWP-800' AND step='dev'")
    ctx.db.run("UPDATE tickets SET status='To Do' WHERE key='TESTWP-800'")

    second = client.post("/v1/work/claim", json={"ticket": "TESTWP-800", "account": "claude-01"}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["project"]["work_minutes"]["hard"] == 120
    assert second.json()["project"]["timeout_retries"] == 2


@pytest.mark.parametrize("base,count", [(60, 0), (60, 1), (60, 2)])
def test_timeout_limit_growth_boundaries(base, count):
    assert retry_hard_minutes(base, count) == 60 + count * 30


@pytest.mark.parametrize("base,count", [(0, 0), (60, -1), (True, 0), (60, True), (60.0, 1)])
def test_timeout_limit_growth_rejects_invalid_values(base, count):
    with pytest.raises(ValueError):
        retry_hard_minutes(base, count)
