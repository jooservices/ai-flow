"""HTTP and database acceptance coverage for the Dashboard conflict measure (JAIF-331)."""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone

from aiflow.v4.control import login
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session


UTC = timezone.utc
TEST_SESSION_SECRET = "jaif-331-test-session-secret-at-least-32-bytes"


def recent_start() -> datetime:
    """Use prior-day UTC noon so fixture runs stay current across midnight boundaries."""
    day = datetime.now(UTC).date() - timedelta(days=1)
    return datetime.combine(day, time(12, 0), tzinfo=UTC)


def add_run(db, *, ticket: str, project: str, outcome: str, started: datetime,
            seconds: int | None, summary: str = "", status: str = "finished", step: str = "verify") -> None:
    ended = started + timedelta(seconds=seconds) if seconds is not None else None
    db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary,"
        "started_at,ended_at) VALUES (%s,%s,%s,%s,'codex-01','codex','cc',1,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), ticket, project, step, status, outcome, summary, started, ended),
    )


def assert_zero_totals(data: dict) -> None:
    expected = {"verifications": 0, "conflicts": 0, "share_percent": 0.0, "seconds": 0,
                "average_seconds": 0, "clean_updates": 0}
    assert data["current"] == expected
    assert data["previous"] == expected


def test_conflict_route_empty_default_shape(client):
    response = client.get("/v1/analytics/conflicts")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"project", "days", "bucket", "definition", "current", "previous", "change", "trend"}
    assert (data["days"], data["bucket"], data["project"]) == (14, "day", "")
    assert data["definition"]
    assert_zero_totals(data)
    assert data["change"] == {"share_points": 0.0, "seconds": 0}
    assert len(data["trend"]) == 14
    assert all(row["verifications"] == row["conflicts"] == row["seconds"] == 0
               and row["share_percent"] == 0.0 for row in data["trend"])


def test_conflict_route_counts_finished_persisted_runs_and_daily_trend(client, ctx):
    started = recent_start()
    add_run(ctx.db, ticket="TESTWP-JAIF331-CONFLICT", project="TESTWP", outcome="rework", started=started,
            seconds=600, summary="conflicts with develop: src/example.py")
    add_run(ctx.db, ticket="TESTWP-JAIF331-MERGED", project="TESTWP", outcome="merged", started=started,
            seconds=300)
    add_run(ctx.db, ticket="TESTWP-JAIF331-RUNNING", project="TESTWP", outcome="rework", started=started,
            seconds=None, summary="conflicts with develop: src/running.py", status="running")
    add_run(ctx.db, ticket="TESTWP-JAIF331-DEV", project="TESTWP", outcome="done", started=started,
            seconds=120, step="dev")

    response = client.get("/v1/analytics/conflicts")

    assert response.status_code == 200
    data = response.json()
    assert data["current"] == {"verifications": 2, "conflicts": 1, "share_percent": 50.0, "seconds": 600,
                               "average_seconds": 600, "clean_updates": 0}
    assert data["previous"] == {"verifications": 0, "conflicts": 0, "share_percent": 0.0, "seconds": 0,
                                "average_seconds": 0, "clean_updates": 0}
    assert data["change"] == {"share_points": 50.0, "seconds": 600}
    assert len(data["trend"]) == 14
    populated = [row for row in data["trend"] if row["period"] == started.date().isoformat()]
    assert len(populated) == 1
    assert populated[0] == {"period": started.date().isoformat(), "verifications": 2, "conflicts": 1,
                            "seconds": 600, "share_percent": 50.0}
    assert sum(row["verifications"] for row in data["trend"]) == 2


def test_conflict_route_validates_days_and_bucket(client):
    for days in (1, 90):
        for bucket in ("day", "week"):
            response = client.get("/v1/analytics/conflicts", params={"days": days, "bucket": bucket})
            assert response.status_code == 200
            assert (response.json()["days"], response.json()["bucket"]) == (days, bucket)

    for days in (0, 91, "not-an-integer", str(10**100)):
        response = client.get("/v1/analytics/conflicts", params={"days": days})
        assert response.status_code == 422
    invalid_bucket = client.get("/v1/analytics/conflicts", params={"bucket": "month"})
    assert invalid_bucket.status_code == 422


def test_conflict_route_sign_in_and_project_scoping(client, ctx):
    started = recent_start()
    for project in ("TESTWP", "OTHER"):
        add_run(ctx.db, ticket=f"{project}-JAIF331-CONFLICT", project=project, outcome="rework", started=started,
                seconds=600, summary="conflicts with develop: src/example.py")
        add_run(ctx.db, ticket=f"{project}-JAIF331-MERGED", project=project, outcome="merged", started=started,
                seconds=300)

    ctx.settings.console_login = False
    open_response = client.get("/v1/analytics/conflicts")
    assert open_response.status_code == 200
    assert open_response.json()["current"] == {"verifications": 4, "conflicts": 2, "share_percent": 50.0,
                                                "seconds": 1200, "average_seconds": 600, "clean_updates": 0}

    ctx.settings.console_login = True
    ctx.settings.console_session_secret = TEST_SESSION_SECRET
    unauthenticated = client.get("/v1/analytics/conflicts")
    assert unauthenticated.status_code == 401
    assert "current" not in unauthenticated.json()

    email = f"conflict-scope-{uuid.uuid4().hex}@example.test"
    login.seed_user(ctx.db, email, "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", (["TESTWP"], email))
    expiry = int(datetime.now(UTC).timestamp()) + 3600
    client.cookies.set(COOKIE_NAME, issue_session(email, "business", expiry, TEST_SESSION_SECRET))

    scoped = client.get("/v1/analytics/conflicts")
    assert scoped.status_code == 200
    assert scoped.json()["current"] == {"verifications": 2, "conflicts": 1, "share_percent": 50.0,
                                        "seconds": 600, "average_seconds": 600, "clean_updates": 0}
    explicit = client.get("/v1/analytics/conflicts", params={"project": "TESTWP"})
    assert explicit.status_code == 200
    assert explicit.json()["current"] == scoped.json()["current"]

    forbidden = client.get("/v1/analytics/conflicts", params={"project": "OTHER"})
    assert forbidden.status_code == 403
    assert "current" not in forbidden.json()

    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s", ([], email))
    empty_scope = client.get("/v1/analytics/conflicts")
    assert empty_scope.status_code == 200
    assert_zero_totals(empty_scope.json())
