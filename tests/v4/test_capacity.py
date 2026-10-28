"""Ticket estimation, quota-aware routing, weekly capacity and estimate error reporting."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import uuid

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import capacity, quota as quota_reporting
from aiflow.v4.control.db import J
from aiflow.v4.control.ui import mount_ui


PAGE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"
UTC = timezone.utc


def node_headers(client, name):
    response = join_node(client, name)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def online(client, name, account):
    headers = node_headers(client, name)
    response = client.post(f"/v1/nodes/{name}/heartbeat",
                          json={"slots_total": 2, "workers": [{"account": account, "state": "idle"}]},
                          headers=headers)
    assert response.status_code == 200
    return headers


def add_history_ticket(db, key, *, tokens, seconds, ticket_type="Bug", labels=None, reworked=False, when=None):
    labels = list(labels or ["size-m", "area-core"])
    started = when or datetime.now(UTC) - timedelta(days=1)
    ended = started + timedelta(seconds=seconds)
    db.run("INSERT INTO tickets (key,project,status,type,labels,summary,description,fields) "
           "VALUES (%s,'TESTWP','Done',%s,%s,%s,'recorded history',%s)",
           (key, ticket_type, labels, key, J({})))
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,started_at,ended_at,"
           "tokens_in,tokens_out) VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',1,'finished','done',%s,%s,%s,0)",
           (str(uuid.uuid4()), key, started, ended, tokens))
    if reworked:
        db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,started_at,ended_at,"
               "tokens_in,tokens_out) VALUES (%s,%s,'TESTWP','verify','codex-01','codex','node1',1,'finished','rework',%s,%s,100,0)",
               (str(uuid.uuid4()), key, ended, ended + timedelta(seconds=20)))


def put_quota(ctx, account_id, *, utilization, tokens=100_000):
    reset = datetime.now(UTC) + timedelta(days=10)
    quota = {"windows": {"weekly": {"utilization": utilization, "quota_tokens": tokens,
                                      "resets_at": reset.isoformat()}}}
    ctx.db.run("UPDATE accounts SET quota=%s,quota_source='run',quota_observed_at=now() WHERE id=%s",
               (J(quota), account_id))


def refresh_forecasts(ctx):
    rows = ctx.db.all("SELECT id,provider,quota,quota_source,quota_observed_at FROM accounts ORDER BY id")
    for account_id, forecast in quota_reporting.assessments(ctx.db, rows).items():
        ctx.db.run("UPDATE accounts SET quota_forecast=%s WHERE id=%s", (J(forecast), account_id))


def test_estimate_uses_ticket_type_size_and_area_history_as_ranges(db):
    add_history_ticket(db, "TESTWP-EST-1", tokens=10_000, seconds=600)
    add_history_ticket(db, "TESTWP-EST-2", tokens=12_000, seconds=900, reworked=True)
    add_history_ticket(db, "TESTWP-EST-3", tokens=14_000, seconds=1_200)

    estimate = capacity.estimate({"type": "Bug", "project": "TESTWP", "labels": ["size-m", "area-core"],
                                  "summary": "Example", "description": "Same scope"}, capacity.history(db))

    assert estimate["source"] == "history"
    assert estimate["basis"] == "type, size and area"
    assert estimate["samples"] == 3
    assert estimate["tokens"]["low"] < estimate["tokens"]["high"]
    assert estimate["time_minutes"]["low"] < estimate["time_minutes"]["high"]
    assert estimate["rework_risk_pct"]["low"] < estimate["rework_risk_pct"]["high"]
    assert (estimate["type"], estimate["size"], estimate["area"]) == ("bug", "M", "core")


def test_estimate_ranges_stay_distinct_after_floors_for_subminute_and_zero_token_history():
    ticket = {"type": "Bug", "project": "TESTWP", "labels": ["size-m", "area-core"]}
    subminute = [{"type": "bug", "size": "M", "area": "core", "tokens": 1_000,
                  "duration_s": 20.0, "reworked": False} for _ in range(3)]
    fast = capacity.estimate(ticket, subminute)
    assert 1.0 <= fast["time_minutes"]["low"] < fast["time_minutes"]["high"]

    zero_tokens = [{"type": "bug", "size": "M", "area": "core", "tokens": 0,
                    "duration_s": 600.0, "reworked": False} for _ in range(3)]
    zero = capacity.estimate(ticket, zero_tokens)
    assert zero["tokens"]["low"] < zero["tokens"]["high"]


def test_capacity_routing_avoids_account_that_will_run_out_and_saves_reason(client, ctx):
    cc = online(client, "cc", "claude-01")
    node1 = online(client, "node1", "codex-01")
    put_quota(ctx, "claude-01", utilization=.99)
    put_quota(ctx, "codex-01", utilization=.05)
    refresh_forecasts(ctx)
    add_ticket(ctx.db, "TESTWP-CAPACITY-1")

    claude_candidates = client.get("/v1/work/candidates?account=claude-01", headers=cc).json()["tickets"]
    codex_candidates = client.get("/v1/work/candidates?account=codex-01", headers=node1).json()["tickets"]

    assert "TESTWP-CAPACITY-1" not in {item["key"] for item in claude_candidates}
    assert "TESTWP-CAPACITY-1" in {item["key"] for item in codex_candidates}
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-CAPACITY-1", "account": "claude-01"},
                       headers=cc).status_code == 409
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-CAPACITY-1", "account": "codex-01"},
                          headers=node1)
    assert claimed.status_code == 200 and claimed.json()["claimed"]

    plan = client.get("/v1/capacity").json()
    decision = next(item for item in plan["decisions"] if item["ticket"] == "TESTWP-CAPACITY-1")
    assert decision["account"] == "codex-01"
    assert "Assigned to codex-01" in decision["reason"]
    assert decision["estimate"]["tokens"]["high"] > decision["estimate"]["tokens"]["low"]
    mount_ui(client.app, ctx)
    detail = client.get("/v1/tickets/TESTWP-CAPACITY-1").json()
    assert detail["capacity_decision"]["account"] == "codex-01"
    assert detail["capacity_decision"]["reason"] == decision["reason"]


def test_candidate_poll_skips_capacity_work_without_reliable_quota_forecasts(client, ctx, monkeypatch):
    cc = online(client, "cc", "claude-01")
    add_ticket(ctx.db, "TESTWP-LEGACY-CAPACITY-1")
    queries = []
    original_all = ctx.db.all

    def record_queries(statement, params=None):
        if "quota_forecast" in statement and "FROM accounts" in statement:
            queries.append(statement)
        return original_all(statement, params)

    monkeypatch.setattr(ctx.db, "all", record_queries)
    monkeypatch.setattr(capacity, "history", lambda _db: pytest.fail("unknown quotas must not estimate capacity"))

    response = client.get("/v1/work/candidates?account=claude-01", headers=cc)

    assert response.status_code == 200
    assert "TESTWP-LEGACY-CAPACITY-1" in {item["key"] for item in response.json()["tickets"]}
    assert len(queries) == 1
    assert "quota_forecast->>'source'='reported'" in queries[0]
    assert "quota_forecast->>'confidence' IN ('medium','high')" in queries[0]


def test_capacity_routing_checks_exhaustion_for_window_without_token_balance(client, ctx):
    cc = online(client, "cc", "claude-01")
    node1 = online(client, "node1", "codex-01")
    now = datetime.now(UTC)
    claude_forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "weekly", "left_tokens": 100_000, "prediction": {}},
        {"name": "five_hour", "left_tokens": None,
         "prediction": {"exhaustion_at": (now + timedelta(minutes=1)).isoformat()}},
    ]}
    codex_forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "weekly", "left_tokens": 100_000,
         "prediction": {"exhaustion_at": (now + timedelta(days=2)).isoformat()}},
    ]}
    ctx.db.run("UPDATE accounts SET quota_forecast=%s WHERE id='claude-01'", (J(claude_forecast),))
    ctx.db.run("UPDATE accounts SET quota_forecast=%s WHERE id='codex-01'", (J(codex_forecast),))
    add_ticket(ctx.db, "TESTWP-UNKNOWN-WINDOW")

    claude_candidates = client.get("/v1/work/candidates?account=claude-01", headers=cc).json()["tickets"]
    codex_candidates = client.get("/v1/work/candidates?account=codex-01", headers=node1).json()["tickets"]

    assert "TESTWP-UNKNOWN-WINDOW" not in {item["key"] for item in claude_candidates}
    assert "TESTWP-UNKNOWN-WINDOW" in {item["key"] for item in codex_candidates}


def test_capacity_reserves_running_estimates_and_unobserved_reported_run_usage(ctx):
    observed_at = datetime.now(UTC) - timedelta(minutes=5)
    forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "weekly", "left_tokens": 100_000, "prediction": {}}]}
    ctx.db.run("INSERT INTO accounts (id,provider,node) VALUES ('claude-01','claude','cc')"
               " ON CONFLICT (id) DO NOTHING")
    ctx.db.run("UPDATE accounts SET quota_forecast=%s,quota_observed_at=%s WHERE id='claude-01'",
               (J(forecast), observed_at))
    run_id = str(uuid.uuid4())
    details = {"quota_reservation": {"tokens": 40_000}}
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,details)"
               " VALUES (%s,'TESTWP-RESERVE-1','TESTWP','dev','claude-01','claude','cc',1,%s)",
               (run_id, J(details)))

    active = capacity.with_active_quota_reservations(ctx.db, {"claude-01": forecast})
    assert active["claude-01"]["windows"][0]["left_tokens"] == 60_000

    ended_at = datetime.now(UTC)
    ctx.db.run("UPDATE runs SET status='finished',ended_at=%s,tokens_in=25_000,tokens_out=5_000 WHERE id=%s",
               (ended_at, run_id))
    stored = ctx.db.one("SELECT status,ended_at,tokens_in,tokens_out FROM runs WHERE id=%s", (run_id,))
    snapshot_at = ctx.db.one("SELECT quota_observed_at FROM accounts WHERE id='claude-01'")["quota_observed_at"]
    assert stored["status"] == "finished" and stored["ended_at"] > snapshot_at
    assert stored["tokens_in"] == 25_000 and stored["tokens_out"] == 5_000
    completed = capacity.with_active_quota_reservations(ctx.db, {"claude-01": forecast})
    assert completed["claude-01"]["windows"][0]["left_tokens"] == 70_000

    ctx.db.run("UPDATE accounts SET quota_observed_at=%s WHERE id='claude-01'", (ended_at + timedelta(seconds=1),))
    reconciled = capacity.with_active_quota_reservations(ctx.db, {"claude-01": forecast})
    assert reconciled["claude-01"]["windows"][0]["left_tokens"] == 100_000


def test_capacity_reserves_lost_run_estimate_until_a_newer_snapshot(ctx):
    observed_at = datetime.now(UTC) - timedelta(minutes=3)
    lost_at = observed_at + timedelta(minutes=1)
    forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "weekly", "left_tokens": 100_000, "prediction": {}}]}
    ctx.db.run("INSERT INTO accounts (id,provider,node) VALUES ('claude-01','claude','cc')"
               " ON CONFLICT (id) DO NOTHING")
    ctx.db.run("UPDATE accounts SET quota_forecast=%s,quota_observed_at=%s WHERE id='claude-01'",
               (J(forecast), observed_at))
    details = {"quota_reservation": {"tokens": 30_000}}
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,started_at,"
               "ended_at,details) VALUES (%s,'TESTWP-LOST-RESERVATION','TESTWP','dev','claude-01','claude','cc',"
               "1,'lost',%s,%s,%s)",
               (str(uuid.uuid4()), lost_at - timedelta(minutes=5), lost_at, J(details)))

    unresolved = capacity.with_active_quota_reservations(ctx.db, {"claude-01": forecast})
    assert unresolved["claude-01"]["windows"][0]["left_tokens"] == 70_000

    ctx.db.run("UPDATE accounts SET quota_observed_at=%s WHERE id='claude-01'", (lost_at + timedelta(seconds=1),))
    reconciled = capacity.with_active_quota_reservations(ctx.db, {"claude-01": forecast})
    assert reconciled["claude-01"]["windows"][0]["left_tokens"] == 100_000


def test_capacity_routing_prefers_middle_tier_to_junior_for_ordinary_work():
    candidates = [{"id": "junior-01", "tier": "junior"}, {"id": "middle-01", "tier": "middle"}]
    forecasts = {
        "junior-01": {"source": "reported", "windows": [{"left_tokens": 100_000, "prediction": {}}]},
        "middle-01": {"source": "reported", "windows": [{"left_tokens": 5_000, "prediction": {}}]},
    }
    estimate = {"size": "M", "tokens": {"low": 1_000, "high": 2_000},
                "time_minutes": {"low": 10, "high": 20}}

    account, fits = capacity.choose_account(candidates, {"labels": [], "priority": "Medium"}, estimate, forecasts)

    assert account == "middle-01"
    assert fits["junior-01"]["fits"] is True and fits["middle-01"]["fits"] is True


def test_weekly_plan_counts_finishable_tickets_and_names_the_limiting_ai(client, ctx):
    put_quota(ctx, "claude-01", utilization=.99)
    put_quota(ctx, "codex-01", utilization=.50)
    add_ticket(ctx.db, "TESTWP-PLAN-1")
    add_ticket(ctx.db, "TESTWP-PLAN-2")

    response = client.get("/v1/capacity")

    assert response.status_code == 200
    plan = response.json()
    assert plan["ready_tickets"] == 2
    assert plan["tickets_finishable"] == 2
    assert plan["quota_capacity_tickets"] > 0
    assert plan["limiting_ai"]["account"] == "claude-01"
    accounts = {item["account"]: item for item in plan["accounts"]}
    assert accounts["claude-01"]["tickets"] < accounts["codex-01"]["tickets"]


def test_estimates_update_with_new_runs_and_completed_ticket_error_is_reported(client, ctx):
    for index, tokens in enumerate((10_000, 12_000, 14_000), start=1):
        add_history_ticket(ctx.db, f"TESTWP-LEARN-{index}", tokens=tokens, seconds=600 + index * 60)
    facts = {"type": "Bug", "project": "TESTWP", "labels": ["size-m", "area-core"], "summary": "target"}
    before = capacity.estimate(facts, capacity.history(ctx.db))
    add_history_ticket(ctx.db, "TESTWP-LEARN-4", tokens=80_000, seconds=4_000)
    after = capacity.estimate(facts, capacity.history(ctx.db))
    assert after["samples"] == before["samples"] + 1
    assert after["tokens"]["high"] != before["tokens"]["high"]

    cc = online(client, "cc", "claude-01")
    add_ticket(ctx.db, "TESTWP-ERROR-1", labels=("aiflow-ready", "size-m", "area-core"))
    ctx.db.run("UPDATE tickets SET type='Bug',summary='target',description='same scope' WHERE key='TESTWP-ERROR-1'")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-ERROR-1", "account": "claude-01"}, headers=cc).json()
    saved = ctx.db.one("SELECT details FROM runs WHERE id=%s", (claim["run_id"],))["details"]["capacity"]["estimate"]
    result = client.post(f"/v1/runs/{claim['run_id']}/result",
                         json={"fence": claim["fence"], "outcome": "done", "tokens_in": 100_000,
                               "tokens_out": 5_000, "summary": "finished"}, headers=cc)
    assert result.status_code == 200
    ctx.db.run("UPDATE tickets SET status='Done' WHERE key='TESTWP-ERROR-1'")

    report = client.get("/v1/capacity").json()
    error = next(item for item in report["errors"] if item["ticket"] == "TESTWP-ERROR-1")
    assert error["estimate"] == saved
    assert error["actual"]["tokens"]["actual"] == 105_000
    assert error["actual"]["tokens"]["within_range"] is False
    assert error["actual"]["tokens"]["error_pct"] > 0


def test_dashboard_exposes_capacity_plan_assignment_reason_and_estimate_error_ui():
    page = PAGE.read_text()
    assert "fetch('/v1/capacity'" in page
    assert "function capacityPanel()" in page
    assert "Capacity plan" in page and "Why tickets were assigned" in page and "Estimate error" in page
    assert "det.capacity_decision" in page and "Why this AI was assigned" in page
    assert "capacity-refresh" in page and "tickets_finishable" in page and "limiting_ai" in page
