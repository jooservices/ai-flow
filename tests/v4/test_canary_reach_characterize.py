"""Pins of pre-JAIF-339 decide edges that remain after the reach change.

Defaults and the idle/health path changed intentionally (see test_canary_reach.py). These cases still use
explicit Thresholds.from_values so the old wait/promote edges stay covered.
"""
from aiflow.v4.control.canary import decide, summarize_runs, Thresholds


def test_characterize_explicit_legacy_thresholds_still_wait_then_promote_at_max():
    idle = {"canary": summarize_runs([]), "baseline": summarize_runs([{"outcome": "done"}] * 10)}
    policy = Thresholds.from_values(20 * 60, 5, 60 * 60, "2", 3)
    assert decide(idle, 20 * 60, policy)["action"] == "wait"
    late = decide(idle, 60 * 60, policy)
    assert late["action"] == "promote"
    assert "too few" in late["reason"]
