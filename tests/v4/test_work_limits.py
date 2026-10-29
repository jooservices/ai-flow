"""Small tickets: the 15-minute target and 30-minute limit (JAIF-371)."""
from __future__ import annotations

import pytest

from aiflow.v4.control.ticket_split_decision import decide_split
from aiflow.v4.core import work_limits
from aiflow.v4.core.controlrepo import Project
from aiflow.v4.core.ticket_splitting import DEFAULT_TARGET_MINUTES


def test_defaults_are_15_30_and_45_minutes():
    assert work_limits.DEFAULTS == {"target": 15, "warn": 30, "hard": 45}
    assert Project(key="K", repo="o/r").work_minutes() == {"target": 15, "warn": 30, "hard": 45}
    assert DEFAULT_TARGET_MINUTES == 15


def test_a_project_can_override_each_value():
    project = Project(key="K", repo="o/r", limits={"work_minutes": {"target": 20, "hard": 45}})
    assert project.work_minutes() == {"target": 20, "warn": 30, "hard": 45}
    assert work_limits.from_setting(None) == work_limits.DEFAULTS
    assert work_limits.from_setting("bad") == work_limits.DEFAULTS


def ticket(size):
    return {"key": "K-1", "type": "Task", "summary": "x", "description": "d", "labels": [f"size-{size.lower()}"], "fields": {}}


@pytest.mark.parametrize("size,split", [("XS", False), ("S", False), ("M", True), ("L", True), ("XL", True)])
def test_without_history_the_size_label_decides(size, split):
    from aiflow.v4.control import capacity
    result = decide_split(ticket(size), [], estimate=lambda t, s: capacity.estimate({**t, "size": size}, s)
                          if False else {"size": size, "source": "size_baseline",
                                         "time_minutes": {"low": 1.0, "high": 999.0}})
    assert result["needs_split"] is split


def test_with_history_the_estimated_high_decides():
    est = lambda t, s: {"size": "S", "source": "history", "time_minutes": {"low": 5.0, "high": 14.0}}
    assert decide_split(ticket("S"), [], estimate=est)["needs_split"] is False
    est = lambda t, s: {"size": "S", "source": "history", "time_minutes": {"low": 5.0, "high": 16.0}}
    assert decide_split(ticket("S"), [], estimate=est)["needs_split"] is True


def test_a_custom_target_moves_the_size_rule():
    est = lambda t, s: {"size": "M", "source": "size_baseline", "time_minutes": {"low": 30.0, "high": 180.0}}
    assert decide_split(ticket("M"), [], target_minutes=30, estimate=est)["needs_split"] is False
    assert decide_split(ticket("M"), [], target_minutes=29, estimate=est)["needs_split"] is True
