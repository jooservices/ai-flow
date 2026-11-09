"""Characterize the JAIF-513 incident counts under explicit example thresholds."""

from aiflow.v4.control.canary import Thresholds, decide


def test_incident_counts_rollback_at_ratio_two_and_wait_at_ratio_four():
    measurements = {
        "canary": {"runs": 7, "infra_failures": 4, "failure_rate": 4 / 7},
        "baseline": {"runs": 16, "infra_failures": 3, "failure_rate": 3 / 16},
    }
    rollback_thresholds = Thresholds.from_values(300, 3, 900, "2", 3)
    wait_thresholds = Thresholds.from_values(300, 3, 900, "4", 3)

    rollback = decide(measurements, 0, rollback_thresholds, installed=True)
    wait = decide(measurements, 0, wait_thresholds, installed=True)

    assert rollback["action"] == "rollback"
    assert rollback["cause"] == "failures"
    assert wait["action"] == "wait"
