"""JAIF-361: needs_split from the existing capacity estimate, without Jira writes."""
from __future__ import annotations

import pytest

from aiflow.v4.control.ticket_split_decision import decide_split
from aiflow.v4.core.ticket_splitting import (
    DEFAULT_TARGET_MINUTES,
    InvalidSplitPlan,
    estimate_needs_split,
)


def _estimate(high: float) -> dict:
    return {"time_minutes": {"low": 10.0, "high": high}, "source": "stub"}


def _child(n: int = 1) -> dict[str, str]:
    return {
        "summary": f"Child ticket {n} for the split",
        "description": f"AC1: Child {n} is testable.\nFiles: aiflow/v4/x.py, tests/v4/test_x.py\nSkeleton: class X with run()\nTest cases: run returns the value\n",
    }


def test_high_above_target_needs_split():
    assert estimate_needs_split(_estimate(31), 30) is True


def test_high_at_or_below_target_does_not_need_split():
    assert estimate_needs_split(_estimate(30), 30) is False
    assert estimate_needs_split(_estimate(29.9), 30) is False


def test_default_target_is_fifteen_minutes():
    assert DEFAULT_TARGET_MINUTES == 15
    assert estimate_needs_split(_estimate(15.1)) is True
    assert estimate_needs_split(_estimate(15)) is False


def test_decide_split_uses_injected_estimate_and_does_not_mutate_ticket():
    ticket = {"key": "JAIF-1", "summary": "Large", "labels": ["aiflow-ready"]}
    snapshot = dict(ticket)
    calls: list[tuple] = []

    def stub(seen_ticket, samples):
        calls.append((seen_ticket, samples))
        return _estimate(90)

    result = decide_split(ticket, [{"tokens": 1}], estimate=stub)

    assert result["needs_split"] is True
    assert result["estimate"] == _estimate(90)
    assert ticket == snapshot
    assert calls == [({"key": "JAIF-1", "summary": "Large", "labels": ["aiflow-ready"]}, [{"tokens": 1}])]


def test_decide_split_false_when_estimate_is_within_target():
    result = decide_split({}, [], target_minutes=30, estimate=lambda ticket, samples: _estimate(15))

    assert result["needs_split"] is False


def test_missing_plan_skips_validate_plan():
    result = decide_split({}, [], plan=None, estimate=lambda ticket, samples: _estimate(40))

    assert result["needs_split"] is True


def test_supplied_valid_plan_is_accepted():
    plan = [_child(1), _child(2)]
    result = decide_split({}, [], plan=plan, estimate=lambda ticket, samples: _estimate(40))

    assert result["needs_split"] is True
    assert plan[0]["summary"].startswith("Child ticket 1")


def test_supplied_invalid_plan_raises_before_returning():
    with pytest.raises(InvalidSplitPlan, match="between 2 and 8"):
        decide_split({}, [], plan=[_child(1)], estimate=lambda ticket, samples: _estimate(40))


def test_missing_high_minutes_is_an_error():
    with pytest.raises(ValueError, match="time_minutes.high"):
        estimate_needs_split({"time_minutes": {"low": 1}})


def test_capacity_estimate_high_minutes_drive_needs_split():
    from aiflow.v4.control import capacity

    large = capacity.estimate({"type": "Story", "summary": "Large", "labels": ["size-L"]}, [])
    small = capacity.estimate({"type": "Story", "summary": "Small", "labels": ["size-XS"]}, [])

    assert estimate_needs_split(large) is True
    assert estimate_needs_split(small) is True  # the range's high end (30 min) is above the 15-minute target
    assert estimate_needs_split(small, 30) is False
