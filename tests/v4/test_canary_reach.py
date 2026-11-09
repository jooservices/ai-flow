"""JAIF-339: canary reach — no restart, settings thresholds, idle judged on health."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4 import cli
from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
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
    mark_build_and_ancestors_live,
    note_pending_build,
    set_settings,
    summarize_runs,
    thresholds_from_settings,
)
from aiflow.v4.control.console_auth import issue_session
from test_console import render_dashboard

SESSION_SECRET = "c" * 40
ADMIN = "canary-owner@example.test"


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
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SESSION_SECRET
    login.seed_user(ctx.db, ADMIN, "super_admin", "Canary Owner")
    app = create_app(ctx)
    app.state.sync_accounts()
    from fastapi.testclient import TestClient
    authenticated = TestClient(app)
    authenticated.cookies.set("aiflow4_session", issue_session(ADMIN, "super_admin", 2_000_000_000,
                                                                  SESSION_SECRET))
    response = authenticated.get("/v1/canary/settings")
    assert response.status_code == 200, response.text
    assert response.json()["minimum_age_s"] == 180
    assert response.json()["defaults"]["maximum_age_s"] == 15 * 60
    updated = authenticated.put("/v1/canary/settings", json={"minimum_age_s": 240,
                                                               "minimum_runs": 4,
                                                               "maximum_age_s": 720})
    assert updated.status_code == 200, updated.text
    reloaded = authenticated.get("/v1/canary/settings")
    assert reloaded.status_code == 200, reloaded.text
    assert (reloaded.json()["minimum_age_s"], reloaded.json()["minimum_runs"],
            reloaded.json()["maximum_age_s"]) == (240, 4, 720)


def test_settings_keep_cli_failure_thresholds(ctx):
    set_settings(ctx.db, 180, 2, 600, "owner@example.com")

    policy = thresholds_from_settings(ctx.db, "0.5", 2)

    assert (policy.minimum_age_s, policy.minimum_runs, policy.maximum_age_s) == (180, 2, 600)
    assert policy.failure_ratio == 0.5
    assert policy.minimum_infra_failures == 2


def test_evaluate_passes_cli_failure_thresholds_alongside_settings(monkeypatch, capsys):
    from aiflow.v4.control import canary

    class FakeDB:
        def one(self, *_args):
            return {"release_version": "candidate"}

        def close(self):
            pass

    captured = {}

    def decide_with_thresholds(_measurements, _elapsed, thresholds, **_kwargs):
        captured["thresholds"] = thresholds
        return {"action": "wait"}

    monkeypatch.setattr("aiflow.v4.control.db.DB", lambda _url: FakeDB())
    monkeypatch.setattr(canary, "measure_registry", lambda *_args: {})
    monkeypatch.setattr(canary, "thresholds_from_settings",
                        lambda db, ratio, minimum: Thresholds.from_values(180, 2, 600, ratio, minimum))
    monkeypatch.setattr(canary, "decide", decide_with_thresholds)
    monkeypatch.setattr(canary, "work_available_on_node", lambda *_args: False)
    monkeypatch.setattr(canary, "node_is_healthy", lambda *_args: True)
    args = SimpleNamespace(canary_action="evaluate", database_url="postgresql://unused", node="node-a",
                           target="candidate", previous="previous", started_at="2026-01-02T03:04:05+00:00",
                           from_settings=True, failure_ratio="0.5", minimum_infra_failures=2)

    cli.run_canary(args, SimpleNamespace(database_url="postgresql://unused"))

    assert captured["thresholds"].minimum_age_s == 180
    assert captured["thresholds"].failure_ratio == 0.5
    assert captured["thresholds"].minimum_infra_failures == 2
    assert '"action":"wait"' in capsys.readouterr().out


def test_evaluate_minimum_age_cli_override_precedes_settings(monkeypatch, capsys):
    from aiflow.v4.control import canary

    class FakeDB:
        def one(self, *_args):
            return {"release_version": "candidate"}

        def close(self):
            pass

    captured = {}

    def decide_with_thresholds(_measurements, _elapsed, thresholds, **_kwargs):
        captured["thresholds"] = thresholds
        return {"action": "wait"}

    monkeypatch.setattr("aiflow.v4.control.db.DB", lambda _url: FakeDB())
    monkeypatch.setattr(canary, "measure_registry", lambda *_args: {})
    monkeypatch.setattr(canary, "thresholds_from_settings",
                        lambda db, ratio, minimum: Thresholds.from_values(180, 2, 900, ratio, minimum))
    monkeypatch.setattr(canary, "decide", decide_with_thresholds)
    monkeypatch.setattr(canary, "work_available_on_node", lambda *_args: False)
    monkeypatch.setattr(canary, "node_is_healthy", lambda *_args: True)
    args = SimpleNamespace(canary_action="evaluate", database_url="postgresql://unused", node="node-a",
                           target="candidate", previous="previous", started_at="2026-01-02T03:04:05+00:00",
                           minimum_age_s=600, from_settings=True, failure_ratio="0.5",
                           minimum_infra_failures=2)

    cli.run_canary(args, SimpleNamespace(database_url="postgresql://unused"))

    assert captured["thresholds"].minimum_age_s == 600
    assert captured["thresholds"].minimum_runs == 2
    assert captured["thresholds"].maximum_age_s == 900
    assert '"action":"wait"' in capsys.readouterr().out


def test_note_pending_requires_a_timestamp_with_an_offset(ctx):
    with pytest.raises(ValueError, match="UTC offset"):
        note_pending_build(ctx.db, "build-without-offset", datetime(2026, 1, 2, 3, 4, 5))

    note_pending_build(ctx.db, "offset-build", datetime.fromisoformat("2026-01-02T03:04:05+07:00"))
    assert ctx.db.one("SELECT merged_at FROM canary_pending WHERE build=%s", ("offset-build",)) is not None


def test_cli_rejects_a_pending_merge_timestamp_without_an_offset(monkeypatch):
    monkeypatch.setattr(cli, "build_ctx", lambda _settings: pytest.fail("database should not be opened"))
    args = SimpleNamespace(canary_action="note-pending", merged_at="2026-01-02T03:04:05")

    with pytest.raises(SystemExit, match="--merged-at must include a UTC offset"):
        cli.run_canary(args, SimpleNamespace())


def test_mark_live_records_pending_ancestors_and_clears_them(ctx):
    now = datetime.now(timezone.utc)
    note_pending_build(ctx.db, "release-a", now - timedelta(minutes=20))
    note_pending_build(ctx.db, "release-b", now - timedelta(minutes=10))
    note_pending_build(ctx.db, "unrelated", now - timedelta(minutes=5))

    live = mark_build_and_ancestors_live(ctx.db, "release-b", ["release-a"], now)

    assert live["recorded"] is True
    assert {row["build"] for row in live["covered_builds"]} == {"release-a"}
    assert ctx.db.one("SELECT count(*) AS n FROM canary_pending")["n"] == 1
    recorded = ctx.db.all("SELECT build, seconds FROM canary_merge_live")
    assert {row["build"] for row in recorded} == {"release-a", "release-b"}
    assert {row["seconds"] for row in recorded} == {600, 1200}


def test_settings_panel_does_not_save_unloaded_defaults_and_can_retry():
    source = Path("aiflow/v4/control/ui/canary_settings.js").read_text()

    assert "state.canaryLoadAttempted = true" in source
    assert "state.canaryError = String(error.message || error); state.canaryLoaded = false" in source
    assert "var locked = state.busy || state.canaryLoading || !state.canaryLoaded" in source
    assert "data-act=\"canary-settings-retry\"" in source
    assert "binding.state.busy || !binding.state.canaryLoaded" in source


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
