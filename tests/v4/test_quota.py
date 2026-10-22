"""Recorded account usage series exercise quota burn, reset forecasts and steady-rate trimming."""
from datetime import datetime, timedelta, timezone

from aiflow.v4.core.quota import account_awareness


UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def recorded_runs(count: int, *, tokens: int = 1000, duration_s: int = 60, start_hours: int = 23) -> list[dict]:
    return [{"ticket_key": f"JAIF-{i + 1}", "model": "gpt-test", "started_at": NOW - timedelta(hours=start_hours - i),
             "ended_at": NOW - timedelta(hours=start_hours - i) + timedelta(seconds=duration_s),
             "duration_s": duration_s, "tokens_in": tokens, "tokens_out": 0, "cost_usd": 0.01}
            for i in range(count)]


def test_steady_burn_excludes_largest_five_percent_and_reports_largest_run():
    runs = recorded_runs(19) + recorded_runs(1, tokens=50000, start_hours=4)
    awareness = account_awareness("codex", {}, runs, [], NOW)
    day_rate = awareness["burn_rates"]["last_day"]
    assert day_rate["runs"] == 20
    assert day_rate["steady_runs"] == 19
    assert day_rate["tokens_per_hour"] == 791.67
    assert awareness["largest_run"]["ticket"] == "JAIF-1"
    assert awareness["largest_run"]["tokens"] == 50000
    assert awareness["confidence"] == "high"


def test_prediction_reports_runout_before_reset_from_recorded_series():
    runs = recorded_runs(20, tokens=1000, start_hours=2)
    quota = {"windows": {"five_hour": {"utilization": .6, "resets_at": (NOW + timedelta(hours=3)).isoformat()}}}
    awareness = account_awareness("claude", quota, runs, [], NOW)
    prediction = awareness["windows"][0]["prediction"]
    assert prediction["status"] == "will run out before reset"
    assert prediction["before_reset"] is True
    assert prediction["exhaustion_at"] is not None


def test_prediction_calls_more_than_twenty_percent_remaining_comfortable():
    runs = recorded_runs(20, tokens=100, start_hours=4)
    quota = {"windows": {"five_hour": {"utilization": .2, "resets_at": (NOW + timedelta(hours=1)).isoformat()}}}
    prediction = account_awareness("claude", quota, runs, [], NOW)["windows"][0]["prediction"]
    assert prediction["status"] == "comfortable"
    assert prediction["remaining_at_reset_pct"] > 20


def test_prediction_at_exactly_twenty_percent_remaining_is_tight():
    runs = [{"ticket_key": f"JAIF-{i + 1}", "started_at": NOW - timedelta(hours=2) + timedelta(minutes=6 * i),
             "ended_at": NOW - timedelta(hours=2) + timedelta(minutes=6 * i, seconds=60),
             "duration_s": 60, "tokens_in": 100, "tokens_out": 0, "cost_usd": .01}
            for i in range(20)]
    quota = {"windows": {"five_hour": {"utilization": .5, "quota_tokens": 9500,
                                         "resets_at": (NOW + timedelta(hours=3)).isoformat()}}}
    prediction = account_awareness("codex", quota, runs, [], NOW)["windows"][0]["prediction"]
    assert prediction["status"] == "tight"
    assert prediction["remaining_at_reset_pct"] == 20


def test_reported_token_capacity_is_not_replaced_by_usage_derived_capacity():
    runs = recorded_runs(20, tokens=1000, start_hours=2)
    quota = {"windows": {"five_hour": {"utilization": .5, "quota_tokens": 100000,
                                         "resets_at": (NOW + timedelta(hours=3)).isoformat()}}}
    window = account_awareness("codex", quota, runs, [], NOW)["windows"][0]
    assert window["left_tokens"] == 50000
    assert window["prediction"]["status"] == "comfortable"


def test_confidence_thresholds_and_uneven_rates():
    assert account_awareness("codex", {}, recorded_runs(7), [], NOW)["confidence"] == "low"
    assert account_awareness("codex", {}, recorded_runs(8), [], NOW)["confidence"] == "medium"
    uneven = recorded_runs(20)
    uneven[-1]["tokens_in"] = 8000
    uneven[-2]["tokens_in"] = 8000
    assert account_awareness("codex", {}, uneven, [], NOW)["confidence"] == "low"


def test_zero_usage_runs_count_toward_rate_spread():
    mixed = recorded_runs(20)
    for run in mixed[:10]:
        run["tokens_in"] = 0
    assert account_awareness("codex", {}, mixed, [], NOW)["confidence"] == "low"


def test_reset_is_learned_from_a_recorded_utilization_drop():
    samples = [
        {"sampled_at": NOW - timedelta(hours=2), "quota": {"windows": {"five_hour": {"utilization": .82}}}},
        {"sampled_at": NOW - timedelta(hours=1), "quota": {"windows": {"five_hour": {"utilization": .08}}}},
    ]
    quota = {"windows": {"five_hour": {"utilization": .1}}}
    awareness = account_awareness("claude", quota, recorded_runs(8), samples, NOW)
    assert awareness["windows"][0]["reset_source"] == "learned"
    assert awareness["windows"][0]["reset_at"] == (NOW + timedelta(hours=4)).isoformat()


def test_missing_provider_quota_is_labelled_as_usage_estimate():
    awareness = account_awareness("grok", {}, recorded_runs(8), [], NOW)
    assert awareness["is_estimate"] is True
    assert awareness["windows"][0]["source"] == "estimate"
    assert awareness["windows"][0]["left_tokens"] >= 0


def test_usage_estimate_learns_reset_from_a_prior_quota_stopped_run():
    reset = NOW + timedelta(hours=2)
    runs = [{"outcome": "quota", "reset_at": reset, "started_at": NOW - timedelta(hours=1),
             "ended_at": NOW - timedelta(minutes=59), "duration_s": 60,
             "tokens_in": 1000, "tokens_out": 0, "cost_usd": 0}]
    window = account_awareness("grok", {}, runs, [], NOW)["windows"][0]
    assert window["reset_source"] == "learned"
    assert window["reset_at"] == reset.isoformat()
