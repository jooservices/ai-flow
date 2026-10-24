"""Daily analytics (TESTFLOW-44): /v1/analytics/daily aggregates runs per UTC day, project and account, and the
Delivery view draws it as a table and an inline SVG chart."""
import re
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"


def day(back: int) -> datetime:
    """01:00 UTC on the day `back` days before today (UTC)."""
    today = datetime.now(timezone.utc).date()
    return datetime.combine(today - timedelta(days=back), time(1, 0), tzinfo=timezone.utc)


def add_run(db, *, back: int, account="claude-01", project="TESTWP", step="dev", outcome="done", seconds=600,
            tokens_in=1000, tokens_out=200, cost=0.5, status="finished"):
    started = day(back)
    ended = started + timedelta(seconds=seconds) if seconds is not None else None
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, node, fence, status, outcome,"
           " started_at, ended_at, tokens_in, tokens_out, cost_usd) VALUES (%s,%s,%s,%s,%s,'claude','cc',1,%s,%s,%s,%s,%s,%s,%s)",
           (str(uuid.uuid4()), f"{project}-1", project, step, account, status, outcome, started, ended,
            tokens_in, tokens_out, cost))


def rows_by_key(client, **params):
    r = client.get("/v1/analytics/daily", params=params)
    assert r.status_code == 200
    return {(x["day"], x["project"], x["account"]): x for x in r.json()["rows"]}


def test_daily_rows_aggregate_runs_per_day_project_and_account(client, ctx):
    db = ctx.db
    # today: claude-01 on TESTWP has done, merged, rework, two failures of different kinds and a running run
    add_run(db, back=0, outcome="done", seconds=300, tokens_in=1000, tokens_out=100, cost=0.25)
    add_run(db, back=0, step="verify", outcome="merged", seconds=600, tokens_in=2000, tokens_out=200, cost=0.5)
    add_run(db, back=0, step="verify", outcome="rework", seconds=900, tokens_in=3000, tokens_out=300, cost=0.75)
    add_run(db, back=0, outcome="failed", seconds=1200, tokens_in=10, tokens_out=1, cost=0.01)
    add_run(db, back=0, outcome="timeout", seconds=3600, tokens_in=0, tokens_out=0, cost=0)
    add_run(db, back=0, outcome="", status="running", seconds=None, tokens_in=0, tokens_out=0, cost=0)
    # today, another account and another project
    add_run(db, back=0, account="codex-01", outcome="done", seconds=120, cost=0.1)
    add_run(db, back=0, project="OTHER", outcome="blocked", seconds=60, cost=0.2)
    # earlier days
    add_run(db, back=3, outcome="done", seconds=100, cost=1)
    add_run(db, back=3, outcome="lost", seconds=300, cost=2)
    add_run(db, back=13, outcome="merged", step="verify", seconds=50)
    add_run(db, back=14, outcome="done")  # outside a 14-day window
    add_run(db, back=40, outcome="done")

    rows = rows_by_key(client, days=14)
    d0, d3, d13 = (day(b).date().isoformat() for b in (0, 3, 13))
    assert set(rows) == {(d0, "TESTWP", "claude-01"), (d0, "TESTWP", "codex-01"), (d0, "OTHER", "claude-01"),
                         (d3, "TESTWP", "claude-01"), (d13, "TESTWP", "claude-01")}

    t = rows[(d0, "TESTWP", "claude-01")]
    assert (t["runs"], t["done"], t["merged"], t["rework"], t["failed"]) == (6, 1, 1, 1, 2)
    assert (t["tokens_in"], t["tokens_out"]) == (6010, 601)
    assert abs(t["cost_usd"] - 1.51) < 1e-6
    assert t["median_run_s"] == 900.0  # finished runs only: 300, 600, 900, 1200, 3600

    assert rows[(d0, "OTHER", "claude-01")]["failed"] == 1  # blocked counts as failed
    assert rows[(d0, "TESTWP", "codex-01")]["median_run_s"] == 120.0
    e = rows[(d3, "TESTWP", "claude-01")]
    assert (e["runs"], e["done"], e["failed"], e["cost_usd"], e["median_run_s"]) == (2, 1, 1, 3.0, 200.0)

    # newest day first, then project and account
    order = [(x["day"], x["project"], x["account"]) for x in client.get("/v1/analytics/daily").json()["rows"]]
    assert order == [(d0, "OTHER", "claude-01"), (d0, "TESTWP", "claude-01"), (d0, "TESTWP", "codex-01"),
                     (d3, "TESTWP", "claude-01"), (d13, "TESTWP", "claude-01")]


def test_usage_can_be_aggregated_by_account_ai_machine_project_and_ticket(client, ctx):
    now = datetime.now(timezone.utc)
    records = [
        ("ALPHA-1", "ALPHA", "claude-01", "claude", "cc", 100, 20, .01),
        ("BETA-2", "BETA", "codex-01", "codex", "node1", 300, 40, .03),
    ]
    for ticket, project, account, provider, node, tokens_in, tokens_out, cost in records:
        started, ended = now - timedelta(minutes=20), now - timedelta(minutes=10)
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
                    "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
                    " VALUES (%s,%s,%s,'dev',%s,%s,'model',%s,1,'finished','done',%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), ticket, project, account, provider, node, started, ended,
                     tokens_in, tokens_out, cost))

    expected = {"account": {"claude-01", "codex-01"}, "ai": {"claude", "codex"},
                "machine": {"cc", "node1"}, "project": {"ALPHA", "BETA"}, "ticket": {"ALPHA-1", "BETA-2"}}
    for dimension, keys in expected.items():
        response = client.get(f"/v1/analytics/usage?days=1&group_by={dimension}")
        assert response.status_code == 200
        rows = response.json()["rows"]
        assert {row["key"] for row in rows} == keys
        assert sum(row["tokens"] for row in rows) == 460
        assert sum(row["runs"] for row in rows) == 2
        assert sum(row["duration_s"] for row in rows) == 1200
        assert abs(sum(row["cost_usd"] for row in rows) - .04) < 1e-6


def test_days_window_project_filter_and_validation(client, ctx):
    add_run(ctx.db, back=0)
    add_run(ctx.db, back=1)
    add_run(ctx.db, back=1, project="OTHER")
    assert {k[0] for k in rows_by_key(client, days=1)} == {day(0).date().isoformat()}
    assert {k[1] for k in rows_by_key(client, days=2, project="OTHER")} == {"OTHER"}
    assert client.get("/v1/analytics/daily").json()["days"] == 14
    assert client.get("/v1/analytics/daily", params={"days": 0}).status_code == 422
    assert client.get("/v1/analytics/daily", params={"days": 91}).status_code == 422
    assert client.get("/v1/analytics/daily").json()["rows"][0]["median_run_s"] == 600.0


def test_no_runs_gives_no_rows(client):
    assert client.get("/v1/analytics/daily", params={"days": 14}).json()["rows"] == []


def test_daily_usage_groups_real_tokens_and_cost_by_provider_and_utc_day(client, ctx):
    add_run(ctx.db, back=0, account="claude-01", tokens_in=100, tokens_out=25, cost=.02)
    now = day(0)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
               "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
               " VALUES (%s,'TESTWP-2','TESTWP','verify','codex-01','codex','node1',1,'finished','done',%s,%s,300,75,.03)",
               (str(uuid.uuid4()), now, now + timedelta(seconds=60)))
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
               "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
               " VALUES (%s,'TESTWP-3','TESTWP','qa','reviewer-console','human','control',1,'finished','done',%s,%s,90,10,.01)",
               (str(uuid.uuid4()), now, now + timedelta(seconds=60)))
    rows = client.get("/v1/analytics/usage/daily", params={"days": 1}).json()["rows"]
    by_provider = {row["key"]: row for row in rows}
    assert set(by_provider) == {"claude", "codex"}
    assert by_provider["claude"]["day"] == day(0).date().isoformat()
    assert (by_provider["claude"]["tokens"], by_provider["claude"]["runs"], by_provider["claude"]["cost_usd"]) == (125, 1, .02)
    assert (by_provider["codex"]["tokens"], by_provider["codex"]["runs"], by_provider["codex"]["cost_usd"]) == (375, 1, .03)
    assert client.get("/v1/analytics/usage/daily", params={"days": 0}).status_code == 422


def test_delivery_view_has_analytics_panel_with_table_and_inline_svg_chart():
    page = PAGE.read_text()
    assert "/v1/analytics/daily?days=' + ANALYTICS_DAYS" in page and "ANALYTICS_DAYS = 14" in page
    assert "h += analyticsPanel();" in re.search(r"function renderDBody\(\)(.*?)\n}\n", page, re.S).group(1)
    panel = re.search(r"function analyticsPanel\(\)(.*?)\n}\n", page, re.S).group(1)
    for col in ("Day", "Account", "Runs", "Done", "Merged", "Rework", "Failed", "Tokens in", "Tokens out", "Cost", "Median run"):
        assert f"<th>{col}</th>" in panel
    chart = re.search(r"function analyticsChart\(rows\)(.*?)\n}\n", page, re.S).group(1)
    assert "<svg" in chart and "<rect" in chart and "role=\"img\"" in chart
    for k in ("ok", "rework", "failed", "other"):
        assert f"'{k}'" in chart
    # No chart library: only the same-origin licence notice asset and the inline console script load.
    assert page.count('<script src="/console-assets/licensing-notice.js"></script>') == 1 and page.count("<script>") == 1
