"""Literal characterization cases for the account quota awareness contract."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiflow.v4.core import quota
from aiflow.v4.core.quota import account_awareness


FIXTURE = Path(__file__).with_name("fixtures") / "account_awareness_parity.jsonl"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _cases():
    with FIXTURE.open(encoding="utf-8") as fixture:
        return [json.loads(line) for line in fixture if line.strip()]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_account_awareness_matches_recorded_output(case):
    actual = account_awareness(
        case["provider"], case["quota"], case["runs"], case["samples"],
        datetime.fromisoformat(case["now"]),
    )

    assert actual == case["expected"]


def test_characterization_table_covers_at_least_twenty_inputs():
    assert len(_cases()) >= 20


def test_awareness_periods_names_hour_day_and_week_starts():
    assert quota._awareness_periods(NOW) == (
        NOW - timedelta(hours=1), NOW - timedelta(hours=24), NOW - timedelta(days=7),
    )


def test_base_rates_exposes_the_three_awareness_periods():
    hour, day, week = quota._awareness_periods(NOW)

    assert set(quota._base_rates([], NOW, hour, day, week)) == {"last_hour", "last_day", "whole_window"}


def test_estimated_window_builds_a_weekly_estimate():
    hour, day, week = quota._awareness_periods(NOW)
    rates = quota._base_rates([], NOW, hour, day, week)

    report = quota._estimated_window([], [], NOW, "low", rates)

    assert report["name"] == "estimated_week"
    assert report["source"] == "estimate"
    assert report["reset_source"] == "estimate"


def test_reported_reset_prefers_a_future_provider_reset():
    reset = NOW + timedelta(hours=2)

    assert quota._reported_reset("five_hour", {"resets_at": reset.isoformat()}, [], [], NOW, 5) == (
        reset, "reported",
    )


def test_reported_usage_sums_tokens_and_cost_for_the_window():
    runs = [{"started_at": (NOW - timedelta(hours=1)).isoformat(), "tokens_in": 100, "tokens_out": 20,
             "cost_usd": 0.25}]

    assert quota._reported_usage(runs, NOW - timedelta(hours=2), NOW) == (120, 0.25)


def test_reported_capacity_accepts_provider_aliases_and_ignores_bad_values():
    assert quota._reported_capacity({"limit_tokens": "2500"}) == 2500
    assert quota._reported_capacity({"quota_tokens": "unknown"}) is None


def test_reported_fraction_rates_uses_provider_sample_delta():
    samples = [
        {"sampled_at": (NOW - timedelta(hours=1)).isoformat(),
         "quota": {"windows": {"five_hour": {"utilization": .2}}}},
        {"sampled_at": NOW.isoformat(), "quota": {"windows": {"five_hour": {"utilization": .3}}}},
    ]

    assert quota._reported_fraction_rates(samples, "five_hour", .3, None, NOW, 5) == pytest.approx((.1, .1))


def test_effective_capacity_uses_recorded_usage_when_provider_omits_capacity():
    assert quota._effective_capacity(None, .5, 1000) == 2000
    assert quota._effective_capacity(5000, .5, 1000) == 5000


def test_reported_window_keeps_provider_window_shape():
    report = quota._reported_window("five_hour", {"utilization": .5}, [], [], NOW, "low",
                                    NOW - timedelta(hours=1), NOW - timedelta(hours=24))

    assert report["name"] == "five_hour"
    assert report["source"] == "reported"
    assert report["used_pct"] == 50


def test_reported_windows_skips_non_dictionary_values():
    reports = quota._reported_windows({"five_hour": {"utilization": .5}, "ignored": "bad"},
                                      [], [], NOW, "low", NOW - timedelta(hours=1),
                                      NOW - timedelta(hours=24))

    assert [report["name"] for report in reports] == ["five_hour"]


def test_largest_run_returns_summary_for_highest_context_usage():
    runs = [{"started_at": NOW.isoformat(), "tokens_in": 100, "tokens_out": 25,
             "ticket_key": "JAIF-100", "model": "model-test", "duration_s": 60}]

    assert quota._largest_run(runs, NOW - timedelta(days=7)) == {
        "ticket": "JAIF-100", "model": "model-test", "tokens": 125, "context_tokens": 100,
        "cost_usd": 0.0, "duration_s": 60.0,
    }


def test_weekly_usage_sums_in_window_values():
    runs = [{"started_at": NOW.isoformat(), "tokens_in": 100, "tokens_out": 25, "cost_usd": .25}]

    assert quota._weekly_usage(runs, NOW - timedelta(days=7)) == {
        "tokens_in": 100, "tokens_out": 25, "tokens": 125, "cost_usd": .25, "runs": 1,
    }


def test_sort_awareness_inputs_orders_history_and_drops_future_runs():
    runs = [{"started_at": (NOW - timedelta(hours=1)).isoformat()},
            {"started_at": (NOW + timedelta(hours=1)).isoformat()},
            {"started_at": (NOW - timedelta(hours=2)).isoformat()}]
    samples = [{"sampled_at": NOW.isoformat()},
               {"sampled_at": (NOW - timedelta(hours=1)).isoformat()}]

    ordered_runs, ordered_samples = quota._sort_awareness_inputs(runs, samples, NOW)

    assert [run["started_at"] for run in ordered_runs] == [runs[2]["started_at"], runs[0]["started_at"]]
    assert [sample["sampled_at"] for sample in ordered_samples] == [samples[1]["sampled_at"], samples[0]["sampled_at"]]


def test_awareness_result_composes_sections_and_at_risk_status():
    windows = [{"prediction": {"status": "will run out before reset"}}]

    result = quota._awareness_result(True, 3, "medium", .12345, {}, [], NOW - timedelta(days=7), NOW, windows)

    assert result["source"] == "estimate"
    assert result["rate_spread_ratio"] == .123
    assert result["at_risk"] is True
    assert result["windows"] == windows
