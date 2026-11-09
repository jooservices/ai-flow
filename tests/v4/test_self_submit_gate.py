"""A development run records its local check and verification dispatch accepts green submissions only."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.adapters import RunResult, RunSpec
from aiflow.v4.control.features.work.eligibility_candidates import (
    _green_dev_submission,
    _verify_candidate_ready,
)
from aiflow.v4.control.features.work.repository import WorkRepository
from aiflow.v4.core.local_gate import GateResult, StepResult
from aiflow.v4.worker import dev, gate_step


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (GateResult([StepResult("check-1", "passed")]),
         {"status": "green", "steps": [{"name": "check-1", "status": "passed"}]}),
        (GateResult([StepResult("check-1", "failed")]),
         {"status": "red", "steps": [{"name": "check-1", "status": "failed"}]}),
        (GateResult([StepResult("check-1", "timeout")]),
         {"status": "incomplete", "steps": [{"name": "check-1", "status": "timeout"}]}),
    ],
)
def test_local_gate_result_record_is_short_and_has_a_dispatch_status(result, expected):
    assert gate_step.result_record(result) == expected


def test_incomplete_gate_failure_text_names_the_steps_that_did_not_finish():
    result = GateResult([StepResult("db-check", "missing_env"), StepResult("slow", "timeout")])

    assert gate_step.failure_text(result) == "checks did not finish: db-check (missing_env), slow (timeout)"


def test_latest_dev_run_query_includes_active_and_failed_runs():
    calls = []
    expected = {"status": "running", "outcome": "", "account_id": "dev", "pr_url": "", "details": {}}
    repository = WorkRepository(SimpleNamespace(one=lambda query, params: calls.append((query, params)) or expected))

    actual = repository.latest_dev_run("JAIF-1")

    assert actual is expected
    assert "step='dev' ORDER BY started_at DESC,id DESC LIMIT 1" in calls[0][0]
    assert calls[0][1] == ("JAIF-1",)


def test_green_dev_submission_requires_a_finished_done_run_with_a_pr_and_gate_record():
    green = {"status": "finished", "outcome": "done", "account_id": "dev", "pr_url": "url",
             "details": {"local_gate": {"status": "green"}}}

    assert _green_dev_submission(green)
    assert not _green_dev_submission(None)
    assert not _green_dev_submission({**green, "status": "running"})
    assert not _green_dev_submission({**green, "outcome": "failed"})
    assert not _green_dev_submission({**green, "pr_url": ""})
    assert not _green_dev_submission({**green, "details": {}})
    assert not _green_dev_submission({**green, "details": {"local_gate": {"status": "red"}}})


def test_verify_dispatch_refuses_missing_or_red_gate_and_accepts_green_latest_run():
    ticket = {"key": "JAIF-1"}
    row = {"status": "finished", "outcome": "done", "account_id": "dev", "pr_url": "url",
           "details": {"local_gate": {"status": "green"}}}
    scope = SimpleNamespace(
        ctx=SimpleNamespace(db=SimpleNamespace(one=lambda *_args: row)),
        needs_missing_container_runtime=lambda *_args: False,
        held=set(), account=SimpleNamespace(id="reviewer"),
        explicit_only_blocks=lambda _ticket: False, allowed=lambda *_args: True,
    )

    assert _verify_candidate_ready(scope, ticket, set())
    row["details"] = {}
    assert not _verify_candidate_ready(scope, ticket, set())
    row["details"] = {"local_gate": {"status": "red"}}
    assert not _verify_candidate_ready(scope, ticket, set())
    row["details"] = {"local_gate": {"status": "green"}}
    row["status"] = "running"
    assert not _verify_candidate_ready(scope, ticket, set())


def test_worker_records_a_green_result_before_it_can_publish(monkeypatch, tmp_path):
    run = SimpleNamespace(
        heartbeat=SimpleNamespace(progress={}), project={"checks": ["pytest"]}, worktree=tmp_path,
        claim=SimpleNamespace(ticket={"key": "JAIF-1", "summary": "Gate result"}), env={}, run_as=[],
        result={"details": {}},
    )
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: GateResult([StepResult("check-1", "passed")]))

    outcome = dev.run_local_gate(run)

    assert outcome == ""
    assert run.result["details"]["local_gate"] == {
        "status": "green", "steps": [{"name": "check-1", "status": "passed"}],
    }


@pytest.mark.parametrize(
    ("gate_result", "status"),
    [
        (GateResult([StepResult("check-1", "failed", tail="assertion failed")]), "red"),
        (GateResult([StepResult("check-1", "timeout")]), "incomplete"),
    ],
)
def test_worker_records_and_refuses_red_or_incomplete_runs(monkeypatch, tmp_path, gate_result, status):
    calls, gate_calls = [], []
    run = SimpleNamespace(
        heartbeat=SimpleNamespace(progress={}), project={"checks": ["pytest"]}, worktree=tmp_path,
        claim=SimpleNamespace(ticket={"key": "JAIF-1", "summary": "Gate result", "type": "Story"},
                              identity=("AI Flow", "aiflow@example.test")), env={}, run_as=[],
        result={"details": {}}, git=SimpleNamespace(run=lambda *_args, **_kwargs: "head",
                                                     changed=lambda _path: [],
                                                     commit_all=lambda *_args: "repair-head"),
        response=RunResult(), summary="Original work",
        srv_root=str(tmp_path), key="JAIF-1", failed_work_commits=[],
    )
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: gate_calls.append("gate") or gate_result)
    if gate_result.failed:
        monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
            spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
        monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(
            target, "response", RunResult(final_text="RESULT: FAIL", verdict="FAIL")) or calls.append("repair"))
    monkeypatch.setattr(dev, "report_gate_failure", lambda *_args: calls.append(_args[1:]) or "refused")

    outcome = dev.run_local_gate(run)

    assert outcome == "refused"
    assert run.result["details"]["local_gate"]["status"] == status
    assert gate_calls == ["gate"]
    assert ("repair" in calls) is bool(gate_result.failed)
    report = next(value for value in calls if isinstance(value, tuple))
    assert report[1] == "gate_failed"


def test_unconfigured_checks_are_recorded_as_missing_and_refused(monkeypatch):
    run = SimpleNamespace(heartbeat=SimpleNamespace(progress={}), project={}, result={"details": {}})
    monkeypatch.setattr(dev, "report_gate_failure", lambda _run, _reason, _kind: "refused")

    outcome = dev.run_local_gate(run)

    assert outcome == "refused"
    assert run.result["details"]["local_gate"] == {"status": "missing", "steps": []}


def test_publish_stops_after_a_refused_local_gate(monkeypatch):
    calls = []

    class Git:
        def commit_all(self, *_args):
            calls.append("commit")

        def push(self, *_args):
            calls.append("push")

    run = SimpleNamespace(
        heartbeat=SimpleNamespace(progress={}), claim=SimpleNamespace(identity=("Dev", "dev@example.test")),
        summary="done", worktree=Path("/work"), git=Git(), changed=["file.py"], result={"details": {}},
        failed_work_commits=[],
    )
    monkeypatch.setattr(dev, "commit_message", lambda *_args: "message")
    monkeypatch.setattr(dev, "run_local_gate", lambda _run: {"outcome": "failed"})
    monkeypatch.setattr(dev.mechanical, "refresh_base", lambda _run: calls.append("refresh") or None)

    result = dev.publish_changes(run)

    assert result == {"outcome": "failed"}
    assert calls == ["commit", "refresh"]


def test_existing_pull_request_reaches_the_refresh_and_gate_publish_stage(monkeypatch, tmp_path):
    calls = []
    run = SimpleNamespace(
        api=SimpleNamespace(call=lambda *_args, **_kwargs: {"found": True, "url": "pr"}),
        claim=SimpleNamespace(run_id="run-1", fence=1, ticket={"key": "JAIF-1", "summary": "Gate"}),
        repo="org/repo", branch="JAIF-1-gate", branch_commit_count=1,
        project={"checks": ["pytest"]}, heartbeat=SimpleNamespace(progress={}), worktree=tmp_path,
        env={}, run_as=[], result={"details": {}},
    )
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: GateResult([StepResult("check-1", "passed")]))
    monkeypatch.setattr(dev, "publish_existing_work", lambda _run, pr: calls.append(pr) or {"outcome": "done"})

    outcome = dev.publish_no_changes(run)

    assert outcome == {"outcome": "done"}
    assert calls == [{"found": True, "url": "pr"}]
