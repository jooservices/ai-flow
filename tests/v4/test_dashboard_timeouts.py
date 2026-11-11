"""JAIF-420: Dashboard timeout analytics, route, and Work-tab panel."""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

import pytest

from aiflow.v4.control import analytics, login
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.ui import build_console_page


UTC = timezone.utc


def day(back: int) -> datetime:
    today = datetime.now(UTC).date()
    return datetime.combine(today - timedelta(days=back), time(1, 0), tzinfo=UTC)


def add_dev_run(db, *, ticket: str, project: str = "TESTWP", outcome: str = "done",
                error_kind: str = "", status: str = "finished", step: str = "dev",
                seconds: int | None = 600) -> None:
    started = day(0)
    ended = None if seconds is None else started + timedelta(seconds=seconds)
    db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
        "error_kind,started_at,ended_at) VALUES (%s,%s,%s,%s,'claude-01','claude','cc',1,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), ticket, project, step, status, outcome, error_kind, started, ended),
    )


def test_timeouts_reject_invalid_days(db):
    with pytest.raises(ValueError):
        analytics.timeouts(db, 0)
    with pytest.raises(ValueError):
        analytics.timeouts(db, 91)
    with pytest.raises(ValueError):
        analytics.timeouts(db, True)  # type: ignore[arg-type]


def test_timeouts_are_zero_when_no_developer_runs_exist(db):
    data = analytics.timeouts(db, 30)
    assert data["days"] == 30
    assert (data["developer_runs"], data["timeouts"], data["timeout_share_pct"], data["target_share_pct"]) == (
        0, 0, 0.0, 10.0)
    assert data["daily"] == []
    assert data["tickets"] == []


def test_dashboard_timeouts_include_daily_ticket_and_developer_share_metrics(client, ctx):
    add_dev_run(ctx.db, ticket="TESTWP-700", outcome="timeout")
    add_dev_run(ctx.db, ticket="TESTWP-700", outcome="failed", error_kind="timeout")
    add_dev_run(ctx.db, ticket="TESTWP-701", outcome="done")
    add_dev_run(ctx.db, ticket="TESTWP-702", outcome="", status="running", seconds=None)
    add_dev_run(ctx.db, ticket="TESTWP-700", step="verify", outcome="timeout")
    add_dev_run(ctx.db, ticket="OTHER-700", project="OTHER", outcome="timeout")

    response = client.get("/v1/dashboard/timeouts", params={"days": 1})
    assert response.status_code == 200
    data = response.json()
    assert (data["developer_runs"], data["timeouts"], data["timeout_share_pct"], data["target_share_pct"]) == (
        4, 3, 75.0, 10.0)
    assert data["daily"][0]["day"] == day(0).date().isoformat()
    assert (data["daily"][0]["developer_runs"], data["daily"][0]["timeouts"]) == (4, 3)
    tickets = {row["ticket"]: row for row in data["tickets"]}
    assert set(tickets) == {"TESTWP-700", "OTHER-700"}
    assert (tickets["TESTWP-700"]["developer_runs"], tickets["TESTWP-700"]["timeouts"]) == (2, 2)
    assert client.get("/v1/dashboard/timeouts", params={"days": 0}).status_code == 422
    assert client.get("/v1/dashboard/timeouts", params={"days": 91}).status_code == 422


def test_dashboard_timeout_analytics_respect_project_permissions(client, ctx):
    add_dev_run(ctx.db, ticket="TESTWP-710", outcome="timeout")
    add_dev_run(ctx.db, ticket="OTHER-710", project="OTHER", outcome="timeout")
    assert client.get("/v1/dashboard/timeouts").status_code == 200

    ctx.settings.console_login = True
    ctx.settings.console_session_secret = "test-dashboard-timeouts-secret-with-at-least-32-bytes"
    assert client.get("/v1/dashboard/timeouts").status_code == 401
    login.seed_user(ctx.db, "timeouts-limited@example.test", "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["TESTWP"], "timeouts-limited@example.test"))
    client.cookies.set(COOKIE_NAME, issue_session("timeouts-limited@example.test", "business", 2_000_000_000,
                                                  ctx.settings.console_session_secret))
    data = client.get("/v1/dashboard/timeouts").json()
    assert {row["project"] for row in data["tickets"]} == {"TESTWP"}
    assert data["timeouts"] == 1 and data["developer_runs"] == 1


def test_dashboard_timeout_panel_is_injected_and_fetches_the_timeouts_route():
    page = build_console_page()
    assert "function dashboardTimeoutsPanel()" in page
    assert "data-block=\"timeouts\"" in page
    assert "Developer run timeouts" in page
    assert "Timeout share" in page and "By day" in page and "By ticket" in page
    assert "fetch('/v1/dashboard/timeouts?days=30'" in page
    assert "loadDashboardTimeouts(false)" in page
    assert 'data-tone="' in page and "share < 10 ? 'good' : 'warn'" in page
    html = (page.split("function dashboardTimeoutsPanel", 1)[0])
    assert "data-block=\"timeouts\"" not in html  # the panel is not baked into console.html
