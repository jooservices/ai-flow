"""Tests for the failure handling of the impact step."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from aiflow.v4.worker import impact
from test_run_steps import _RecordingAPI, _RecordingGit, _RecordingHeartbeat


def test_unexpected_error_in_the_impact_step_is_reported_as_a_failed_outcome_and_logged(monkeypatch, tmp_path, caplog):
    """An unexpected error inside the guarded steps becomes a failed outcome with its message, and is logged."""
    events: list[str] = []
    api = _RecordingAPI(events)
    claim = SimpleNamespace(run_id="run-1", fence=7, run_token="token", account="worker-1", provider="claude",
                            model="sonnet", effort="", ticket={"key": "SHOP-4", "project": "SHOP"},
                            project={"key": "SHOP", "repo": "org/shop", "base": "develop"}, step="impact",
                            project_tickets=[], context_pack={})

    def failing_prompt(_claim, _spec):
        raise RuntimeError("prompt template is missing")

    monkeypatch.setattr(impact, "Git", _RecordingGit)
    monkeypatch.setattr(impact, "Heartbeat", lambda api, claim, every: _RecordingHeartbeat(api, claim, every, events))
    monkeypatch.setattr(impact, "read_spec", lambda _worktree: "project spec")
    monkeypatch.setattr(impact, "build_prompt", failing_prompt)

    with caplog.at_level(logging.ERROR, logger="aiflow4.impact"):
        result = impact.run_impact(api, claim, str(tmp_path), "https://control.test", 30, [])

    assert result == {"fence": 7, "outcome": "failed", "summary": "impact worker error: prompt template is missing"}
    assert "impact step failed" in caplog.text
