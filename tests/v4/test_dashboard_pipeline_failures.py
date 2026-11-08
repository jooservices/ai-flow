"""Dashboard first-run pipeline failures, project scoping, and the Command Center panel."""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

from aiflow.v4.control import login
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.db import J
from aiflow.v4.control.ui import build_console_page


UTC = timezone.utc


def day(back: int) -> datetime:
    return datetime.combine(datetime.now(UTC).date() - timedelta(days=back), time(1, 0), tzinfo=UTC)


def add_verify(db, ticket: str, url: str, started: datetime, ci: list[dict], project: str = "TESTWP",
               check_gaps: list[dict] | None = None) -> None:
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,started_at,"
           "ended_at,pr_url,details) VALUES (%s,%s,%s,'verify','TESTWP_test','claude','cc',1,'finished','rework',"
           "%s,%s,%s,%s)", (str(uuid.uuid4()), ticket, project, started, started + timedelta(minutes=2), url,
                            J({"ci": ci, "check_gaps": check_gaps or []})))


def test_dashboard_shows_daily_first_pipeline_failures_and_the_failed_job(client, ctx):
    today, yesterday = day(0), day(1)
    add_verify(ctx.db, "TESTWP-801", "https://github.com/org/repo/pull/801", today,
               [{"name": "Documentation", "state": "failure", "url": "https://github.test/job/1"}],
               check_gaps=[{"job": "Documentation", "reason": "not in local gate"}])
    add_verify(ctx.db, "TESTWP-801", "https://github.com/org/repo/pull/801", today + timedelta(hours=1),
               [{"name": "Documentation", "state": "success", "url": ""}])
    add_verify(ctx.db, "TESTWP-802", "https://github.com/org/repo/pull/802", today + timedelta(minutes=5),
               [{"name": "Test (v4)", "state": "success", "url": ""}])
    add_verify(ctx.db, "TESTWP-802", "https://github.com/org/repo/pull/802", today + timedelta(hours=2),
               [{"name": "Test (v4)", "state": "failure", "url": ""}])
    add_verify(ctx.db, "TESTWP-803", "https://github.com/org/repo/pull/803", yesterday,
               [{"name": "Giant files", "state": "failure", "url": ""}])

    response = client.get("/v1/dashboard/pipeline-failures", params={"days": 3})

    assert response.status_code == 200
    data = response.json()
    assert (data["first_runs"], data["first_failures"], data["failure_share_pct"], data["target_share_pct"]) == (
        3, 2, 66.67, 15.0)
    assert data["daily"][0]["day"] == today.date().isoformat()
    assert data["daily"][0]["first_runs"] == 2 and data["daily"][0]["first_failures"] == 1
    assert data["daily"][0]["pull_requests"] == [{"ticket": "TESTWP-801", "project": "TESTWP",
                                                   "url": "https://github.com/org/repo/pull/801",
                                                   "failed_jobs": ["Documentation"],
                                                   "check_gaps": [{"job": "Documentation", "reason": "not in local gate"}]}]
    assert data["daily"][1]["pull_requests"][0]["failed_jobs"] == ["Giant files"]
    assert client.get("/v1/dashboard/pipeline-failures", params={"days": 0}).status_code == 422


def test_dashboard_pipeline_failures_respect_project_permissions(client, ctx):
    add_verify(ctx.db, "TESTWP-811", "https://github.com/org/repo/pull/811", day(0),
               [{"name": "Documentation", "state": "failure"}])
    add_verify(ctx.db, "OTHER-811", "https://github.com/other/repo/pull/811", day(0),
               [{"name": "Security scan", "state": "failure"}], project="OTHER")
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = "dashboard-pipeline-failure-secret-with-32-bytes"
    login.seed_user(ctx.db, "pipeline-viewer@example.test", "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (["TESTWP"], "pipeline-viewer@example.test"))
    client.cookies.set(COOKIE_NAME, issue_session("pipeline-viewer@example.test", "business", 2_000_000_000,
                                                  ctx.settings.console_session_secret))

    data = client.get("/v1/dashboard/pipeline-failures", params={"days": 1}).json()

    assert data["first_runs"] == 1 and data["first_failures"] == 1
    assert {row["project"] for day_row in data["daily"] for row in day_row["pull_requests"]} == {"TESTWP"}


def test_pipeline_failure_panel_is_polled_and_rendered_on_the_dashboard():
    page = build_console_page()
    assert "First pipeline run failures" in page
    assert "Failed job" in page and "Check gap" in page
    assert "pill(tone, 'Target < ' + data.target_share_pct + '%')" in page
    assert "fetch('/v1/dashboard/pipeline-failures?days='" in page
    assert "pipelineFailuresPanel()" in page
