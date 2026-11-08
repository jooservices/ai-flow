"""JAIF-339: canary reach — no restart, settings thresholds, idle judged on health."""
from datetime import datetime, timedelta, timezone

from aiflow.v4.control.canary import (
    DEFAULT_MAXIMUM_AGE_S,
    DEFAULT_MINIMUM_AGE_S,
    DEFAULT_MINIMUM_RUNS,
    Thresholds,
    alert_cause,
    control_canary_status,
    decide,
    get_settings,
    keep_canary_target,
    mark_build_live,
    note_pending_build,
    set_settings,
    summarize_runs,
)
from test_console import render_dashboard


def test_newer_build_does_not_restart_the_canary():
    plan = keep_canary_target("abc1234567", "def9876543")
    assert plan == {"action": "continue", "target": "abc1234567", "pending_newest": "def9876543"}
    idle = keep_canary_target("", "def9876543")
    assert idle == {"action": "start", "target": "def9876543", "pending_newest": ""}


def test_thresholds_defaults_match_settings_screen():
    policy = Thresholds()
    assert policy.minimum_age_s == DEFAULT_MINIMUM_AGE_S == 5 * 60
    assert policy.minimum_runs == DEFAULT_MINIMUM_RUNS == 3
    assert policy.maximum_age_s == DEFAULT_MAXIMUM_AGE_S == 15 * 60


def test_thresholds_are_read_from_settings(ctx, client):
    saved = set_settings(ctx.db, 180, 2, 600, "owner@example.com")
    assert saved["minimum_age_s"] == 180
    assert get_settings(ctx.db)["minimum_runs"] == 2
    response = client.get("/v1/canary/settings")
    assert response.status_code in (200, 401, 403, 503)
    if response.status_code == 200:
        body = response.json()
        assert body["minimum_age_s"] == 180
        assert body["defaults"]["maximum_age_s"] == 15 * 60


def test_lack_of_work_is_judged_by_health_checks():
    idle = {"canary": summarize_runs([]), "baseline": summarize_runs([{"outcome": "done"}] * 5)}
    policy = Thresholds.from_values(5 * 60, 3, 15 * 60, "2", 3)
    assert decide(idle, 5 * 60 - 1, policy, work_available=False, healthy=True)["action"] == "wait"
    promoted = decide(idle, 5 * 60, policy, work_available=False, healthy=True)
    assert promoted["action"] == "promote"
    assert promoted["cause"] == "too_few_runs"
    assert "lack of work" in promoted["reason"]
    assert decide(idle, 5 * 60, policy, work_available=False, healthy=False)["action"] == "wait"
    assert decide(idle, 5 * 60, policy, work_available=True, healthy=True)["action"] == "wait"


def test_alert_cause_names_failures_too_few_runs_and_slow():
    assert alert_cause({"action": "rollback", "reason": "failure rate"}) == "failures"
    assert alert_cause({"action": "promote", "reason": "lack of work; judged on health checks"}) == "too_few_runs"
    assert alert_cause({"action": "wait", "reason": "waiting for the minimum canary time"}) == "slow"


def test_dashboard_status_shows_running_canary_pending_and_age(ctx, tmp_path):
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "CURRENT").write_text("aaa1111111\n")
    (releases / "CANARY.json").write_text('{"target":"bbb2222222","node":"canary"}')
    merged = datetime.now(timezone.utc) - timedelta(minutes=12)
    note_pending_build(ctx.db, "ccc3333333", merged)
    note_pending_build(ctx.db, "bbb2222222", merged - timedelta(minutes=5))
    status = control_canary_status(ctx.db, releases)
    assert status["running_build"] == "aaa1111111"
    assert status["canary_build"] == "bbb2222222"
    assert status["pending_merges"] == 2
    assert status["oldest_pending_age_s"] >= 12 * 60 - 5
    live = mark_build_live(ctx.db, "bbb2222222")
    assert live["recorded"] is True
    assert get_settings(ctx.db)["defaults"]["minimum_runs"] == 3


def test_dashboard_shows_control_plane_canary_labels():
    rendered = render_dashboard({
        "projects": [], "tickets": [], "nodes": [], "accounts": [],
        "active_runs": [], "recent_runs": [], "incidents": [], "events": [],
        "totals": {}, "versions": {},
    })
    for text in ("Control plane canary", "Running build", "Build in canary", "Merged, not live"):
        assert text in rendered
