"""Dashboard delivery-yield analytics for completed developer runs, returns, merges and tokens."""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

import pytest

from aiflow.v4.control import analytics, login
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.ui import build_console_page


UTC = timezone.utc


def today() -> datetime:
    return datetime.combine(datetime.now(UTC).date(), time(1, 0), tzinfo=UTC)


def add_run(db, *, ticket: str, step: str = "dev", outcome: str = "done", project: str = "TESTWP",
            tokens_in: int = 100, tokens_out: int = 50, ended: bool = True,
            started_at: datetime | None = None) -> None:
    started = started_at or today()
    ended_at = started + timedelta(minutes=10) if ended else None
    db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
        "started_at,ended_at,tokens_in,tokens_out) VALUES (%s,%s,%s,%s,'claude-01','claude','cc',1,%s,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), ticket, project, step, "finished" if ended else "running", outcome, started,
         ended_at, tokens_in, tokens_out),
    )


def test_yield_metrics_report_returns_and_per_day_ratios(db):
    for index in range(5):
        add_run(db, ticket=f"TESTWP-{index}", outcome="failed" if index == 4 else "done")
    add_run(db, ticket="TESTWP-0", step="verify", outcome="merged", tokens_in=10, tokens_out=5)
    add_run(db, ticket="TESTWP-1", step="verify", outcome="merged", tokens_in=10, tokens_out=5)
    add_run(db, ticket="TESTWP-2", step="verify", outcome="rework", tokens_in=10, tokens_out=10)
    add_run(db, ticket="TESTWP-3", ended=False, tokens_in=999, tokens_out=999)

    data = analytics.yield_metrics(db, 1)

    assert (data["developer_runs"], data["merged"], data["returns"], data["tokens"]) == (5, 2, 1, 800)
    assert (data["merged_per_100_runs"], data["returns_per_100_runs"]) == (40.0, 20.0)
    assert (data["runs_per_merged"], data["tokens_per_merged"], data["target_merged_per_100"]) == (2.5, 400.0, 40.0)
    assert data["daily"][0]["day"] == today().date().isoformat()
    assert (data["daily"][0]["developer_runs"], data["daily"][0]["returns"]) == (5, 1)


def test_yield_analytics_are_empty_without_runs_and_reject_bad_days(db):
    data = analytics.yield_metrics(db, 14)
    assert data["daily"] == []
    assert data["developer_runs"] == data["merged"] == data["returns"] == data["tokens"] == 0
    assert data["runs_per_merged"] is None and data["tokens_per_merged"] is None
    for days in (0, 91, True):
        with pytest.raises(ValueError):
            analytics.yield_metrics(db, days)


def test_yield_ratios_are_unavailable_when_no_developer_runs_exist(db):
    add_run(db, ticket="TESTWP-21", step="verify", outcome="merged")

    data = analytics.yield_metrics(db, 1)
    page = build_console_page()

    assert data["developer_runs"] == data["daily"][0]["developer_runs"] == 0
    assert data["merged_per_100_runs"] is data["daily"][0]["merged_per_100_runs"] is None
    assert data["returns_per_100_runs"] is data["daily"][0]["returns_per_100_runs"] is None
    assert "(per100 == null ? '—' : per100.toFixed(2))" in page
    assert "(row.merged_per_100_runs == null ? '—'" in page


def test_overall_merged_count_deduplicates_tickets_across_days(db):
    yesterday = today() - timedelta(days=1)
    add_run(db, ticket="TESTWP-22", started_at=yesterday)
    add_run(db, ticket="TESTWP-23", started_at=today())
    add_run(db, ticket="TESTWP-22", step="verify", outcome="merged", started_at=yesterday)
    add_run(db, ticket="TESTWP-22", step="verify", outcome="merged", started_at=today())

    data = analytics.yield_metrics(db, 14)

    assert data["merged"] == 1
    assert sum(row["merged"] for row in data["daily"]) == 2
    assert data["merged_per_100_runs"] == 50.0


def test_yield_route_uses_the_callers_project_scope(client, ctx):
    add_run(ctx.db, ticket="TESTWP-20")
    add_run(ctx.db, ticket="TESTWP-20", step="verify", outcome="merged")
    add_run(ctx.db, ticket="OTHER-20", project="OTHER")
    add_run(ctx.db, ticket="OTHER-20", project="OTHER", step="verify", outcome="merged")

    ctx.settings.console_login = True
    ctx.settings.console_session_secret = "yield-test-session-secret-with-more-than-32-bytes"
    assert client.get("/v1/dashboard/yield").status_code == 401
    login.seed_user(ctx.db, "yield-limited@example.test", "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (["TESTWP"], "yield-limited@example.test"))
    client.cookies.set(COOKIE_NAME, issue_session("yield-limited@example.test", "business", 2_000_000_000,
                                                  ctx.settings.console_session_secret))
    response = client.get("/v1/dashboard/yield", params={"days": 1})

    assert response.status_code == 200
    assert response.json()["developer_runs"] == response.json()["merged"] == 1
    assert client.get("/v1/dashboard/yield", params={"days": 0}).status_code == 422
    assert client.get("/v1/dashboard/yield", params={"days": 91}).status_code == 422


def test_dashboard_yield_panel_is_mounted_and_shows_return_and_yield_counts():
    page = build_console_page()

    assert "function dashboardYieldPanel()" in page
    assert 'data-block="yield"' in page
    assert "Completed developer runs" in page and "Verification returns" in page
    assert "Merged per 100 runs" in page and "Tokens per merged ticket" in page
    assert "fetch('/v1/dashboard/yield?days=14'" in page
    assert "loadDashboardYield(false)" in page


def test_dashboard_yield_load_replaces_its_pending_poll_timer():
    page = build_console_page()

    assert "if (state.pollTimer != null) { clearTimeout(state.pollTimer); state.pollTimer = null; }" in page
    assert "state.pollTimer = setTimeout(function () { state.pollTimer = null; loadDashboardYield(true); }" in page
