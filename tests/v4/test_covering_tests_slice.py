"""JAIF-419: covering tests first, one full suite, reserved time slice."""
from __future__ import annotations

from aiflow.v4.core.work_limits import FULL_SUITE_TIME_SLICE_MINUTES
from aiflow.v4.worker.dev import Claimed, build_prompt


def _claimed():
    project = {"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "base": "develop",
               "checks": ["uv run pytest tests/v4 -q"], "work_minutes": {"hard": 60}}
    return Claimed(run_id="run-1", fence=1, run_token="t", account="claude-01", provider="claude",
                   model="", effort="", ticket={"key": "TESTWP-1", "project": "TESTWP", "summary": "S",
                                                "description": "AC1", "labels": [], "priority": "Medium",
                                                "type": "Bug"},
                   project=project, identity=("bot", "bot@example.test"))


def test_prompt_runs_covering_tests_before_the_full_suite():
    prompt = build_prompt(_claimed(), "TESTWP-1-s")
    covering = prompt.index("cover the changed code first")
    full = prompt.index("full configured suite exactly once")
    assert covering < full
    assert "before the full suite" in prompt


def test_prompt_reserves_a_fifteen_minute_full_suite_slice():
    assert FULL_SUITE_TIME_SLICE_MINUTES == 15
    prompt = build_prompt(_claimed(), "TESTWP-1-s")
    assert f"final {FULL_SUITE_TIME_SLICE_MINUTES}-minute test time slice" in prompt
    assert "exactly once at the end of implementation" in prompt


def test_prompt_accepts_finished_or_unfinished_full_suite_lines():
    prompt = build_prompt(_claimed(), "TESTWP-1-s")
    assert "`FULL SUITE: FINISHED`" in prompt
    assert "`FULL SUITE: UNFINISHED - <why>`" in prompt
    assert "not a reason to withhold RESULT: DONE" in prompt
    assert "RESULT: DONE" in prompt
