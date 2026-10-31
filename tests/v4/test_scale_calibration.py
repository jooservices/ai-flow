"""The scale proof budgets are written for a reference machine; a slower runner scales the time budgets (JAIF-450)."""
from __future__ import annotations

from aiflow.v4.control import scale_proof

MEASURED = {"console_page_load": {"value": 3.2}, "project_picker": {"value": 150.0},
            "ticket_pickup": {"value": 35.3}}


def verdicts(calibration=None):
    report = {"measurements": MEASURED, **({"calibration": calibration} if calibration is not None else {})}
    return {v["key"]: v for v in scale_proof.finalize(report)["budgets"]}


def test_the_reference_machine_keeps_the_written_limits():
    result = verdicts({"factor": 1.0})
    assert not result["ticket_pickup"]["passed"] and result["ticket_pickup"]["effective_limit"] == 30.0
    assert "exceeds the budget of 30 s" in result["ticket_pickup"]["message"]
    assert verdicts()["ticket_pickup"]["effective_limit"] == 30.0  # a report without calibration is not scaled


def test_a_slower_runner_scales_every_time_budget_and_says_so():
    result = verdicts({"factor": 2.0})
    assert result["ticket_pickup"]["passed"] and result["ticket_pickup"]["effective_limit"] == 60.0
    assert result["project_picker"]["effective_limit"] == 400.0 and result["console_page_load"]["effective_limit"] == 4.0
    assert "30 s x 2 for this machine = 60 s" in result["ticket_pickup"]["message"]
    assert result["console_page_load"]["passed"]


def test_the_factor_is_capped_so_a_regression_is_still_caught_on_a_slow_runner():
    assert scale_proof.speed_factor(10.0) == scale_proof.MAX_SPEED_FACTOR
    assert verdicts({"factor": 50})["ticket_pickup"]["effective_limit"] == 30.0 * scale_proof.MAX_SPEED_FACTOR
    assert not scale_proof.finalize({"measurements": {"ticket_pickup": {"value": 120.0}},
                                     "calibration": {"factor": 50}})["passed"]


def test_a_faster_machine_or_a_bad_report_never_tightens_the_budgets():
    assert scale_proof.speed_factor(0.01) == 1.0 and scale_proof.speed_factor() == 1.0
    assert verdicts({"factor": 0.2})["ticket_pickup"]["effective_limit"] == 30.0
    assert verdicts({"factor": "fast"})["ticket_pickup"]["effective_limit"] == 30.0


def test_the_slowest_of_the_two_probes_decides_and_the_report_records_them():
    info = scale_proof.calibration(0.115, 0.23)
    assert info["factor"] == 2.0 and info["before_s"] == 0.115 and info["after_s"] == 0.23
    assert info["reference_s"] == scale_proof.PROBE_REFERENCE_S and info["max_factor"] == scale_proof.MAX_SPEED_FACTOR


def test_the_probe_is_a_fixed_cpu_workload_close_to_its_reference_on_this_machine():
    seconds = scale_proof.cpu_probe(repeats=1)
    assert 0 < seconds < scale_proof.PROBE_REFERENCE_S * 30
