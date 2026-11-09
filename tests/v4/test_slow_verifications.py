"""Dashboard lists tickets waiting for verification longer than 30 minutes (JAIF-444 / JAIF-271 AC7)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from conftest import add_ticket
from aiflow.v4.control import slow_verifications, state
from aiflow.v4.control.db import J
from aiflow.v4.control.features.business import state_view
from aiflow.v4.control.ui import console_template

PR = "https://github.com/jooservices/test-aiflow-wp/pull/7"


def _insert_run(ctx, key, step, account, outcome, *, details=None, minutes_ago=10, pr_url="", summary=""):
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    provider = account.split("-", 1)[0]
    node = "node1" if account == "codex-01" else "cc"
    ctx.db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
        "started_at,ended_at,pr_url,summary,details) "
        "VALUES (%s,%s,'TESTWP',%s,%s,%s,'m',%s,1,'finished',%s,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), key, step, account, provider, node, outcome, started,
         started + timedelta(minutes=1), pr_url, summary, J(details or {})),
    )


def _in_verify(ctx, key="TESTWP-80"):
    add_ticket(ctx.db, key, "In Verify")
    _insert_run(ctx, key, "dev", "opencode-01", "done", pr_url=PR, minutes_ago=60,
                details={"local_gate": {"status": "green"}})


def _no_verdict(ctx, key, account, minutes_ago):
    _insert_run(ctx, key, "verify", account, "failed", details={"no_verdict": True},
                minutes_ago=minutes_ago, summary="no verdict (missing VERDICT line): x")


def test_describe_lists_tried_accounts_and_no_verdict_counts():
    text = slow_verifications.describe({
        "waiting_minutes": 45,
        "accounts": [
            {"account": "claude-01", "runs": 2, "no_verdict": 2, "barred": True},
            {"account": "codex-01", "runs": 1, "no_verdict": 0, "barred": False},
        ],
    })
    assert text.startswith("45 min · tried:")
    assert "claude-01 (no verdict x2, not offered again)" in text
    assert "codex-01" in text and "codex-01 (" not in text


def test_describe_marks_empty_try_list():
    assert slow_verifications.describe({"waiting_minutes": 31, "accounts": []}) == (
        "31 min · tried: no account has tried yet")


def test_dashboard_lists_only_tickets_waiting_longer_than_30_minutes(ctx):
    _in_verify(ctx, "TESTWP-80")
    add_ticket(ctx.db, "TESTWP-81", "In Verify")
    _insert_run(ctx, "TESTWP-81", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=60)
    checked_at = ctx.db.one("SELECT now() AS t")["t"]
    ctx.db.run("UPDATE runs SET ended_at=%s-interval '31 minutes' WHERE ticket_key='TESTWP-80' AND step='dev'",
               (checked_at,))
    ctx.db.run("UPDATE runs SET ended_at=%s-interval '30 minutes' WHERE ticket_key='TESTWP-81' AND step='dev'",
               (checked_at,))
    assert [row["key"] for row in slow_verifications.slow_verifications(ctx.db, now=checked_at)] == ["TESTWP-80"]


def test_dashboard_describes_tried_account_without_no_verdict_details(ctx):
    _in_verify(ctx, "TESTWP-82")
    checked_at = ctx.db.one("SELECT now() AS t")["t"]
    ctx.db.run("UPDATE runs SET ended_at=%s-interval '40 minutes' WHERE ticket_key='TESTWP-82' AND step='dev'",
               (checked_at,))
    _insert_run(ctx, "TESTWP-82", "verify", "claude-01", "quota", details={"no_verdict": False}, minutes_ago=5)

    slow = slow_verifications.slow_verifications(ctx.db, now=checked_at)[0]
    described = slow_verifications.describe(slow)

    assert "claude-01" in described
    assert "no verdict" not in described
    assert "no account has tried yet" not in described


def test_state_snapshot_includes_slow_verifications_and_ticket_line(ctx):
    _in_verify(ctx, "TESTWP-83")
    checked_at = ctx.db.one("SELECT now() AS t")["t"]
    ctx.db.run("UPDATE runs SET ended_at=%s-interval '40 minutes' WHERE ticket_key='TESTWP-83' AND step='dev'",
               (checked_at,))
    _no_verdict(ctx, "TESTWP-83", "claude-01", minutes_ago=5)

    data = state.snapshot(ctx)
    assert data["slow_verifications"][0]["key"] == "TESTWP-83"
    assert data["slow_verifications"][0]["accounts"][0]["account"] == "claude-01"
    assert data["slow_verifications"][0]["accounts"][0]["no_verdict"] == 1
    ticket = next(row for row in data["tickets"] if row["key"] == "TESTWP-83")
    assert "claude-01" in ticket["slow_verify"]


def test_restricted_state_redacts_verifier_account_ids():
    data = {
        "tickets": [{"key": "TESTWP-84", "project": "TESTWP", "slow_verify": "40 min · tried: claude-01"}],
        "slow_verifications": [{
            "key": "TESTWP-84", "project": "TESTWP", "summary": "Wait", "waiting_minutes": 40,
            "accounts": [{"account": "claude-01", "runs": 1, "no_verdict": 1, "barred": False}],
        }],
        "nodes": [{"name": "cc"}], "accounts": [{"id": "claude-01", "machines": []}],
        "sa": {"x": 1}, "role_health": {"roles": [1]}, "version_policy": {"x": 1},
        "incidents": [{"subject": "TESTWP-84"}], "incidents_recent": [],
        "events": [{"ts": "t", "type": "x", "ticket": "TESTWP-84", "account": "claude-01",
                    "node": "cc", "outcome": "o", "reason": "r"}],
        "active_runs": [], "recent_runs": [],
    }
    state_view.hide_machines_and_detail(data)
    assert data["slow_verifications"][0]["accounts"][0]["account"] == "Verifier 1"
    assert "Verifier 1" in data["tickets"][0]["slow_verify"]
    assert "claude-01" not in data["tickets"][0]["slow_verify"]


def test_project_scope_filters_slow_verifications():
    data = {
        "tickets": [
            {"key": "TESTWP-85", "project": "TESTWP", "slow_verify": "a"},
            {"key": "OTHER-85", "project": "OTHER", "slow_verify": "b"},
        ],
        "slow_verifications": [
            {"key": "TESTWP-85", "project": "TESTWP", "summary": "a", "waiting_minutes": 40, "accounts": []},
            {"key": "OTHER-85", "project": "OTHER", "summary": "b", "waiting_minutes": 41, "accounts": []},
        ],
        "projects": [{"key": "TESTWP"}, {"key": "OTHER"}],
        "active_runs": [], "recent_runs": [], "events": [], "nodes": [], "accounts": [],
        "incidents": [], "incidents_recent": [], "release_readiness": [1], "release_progress": [1],
        "rollout": {"x": 1}, "sa": {"x": 1}, "version_policy": {"x": 1}, "totals": {},
    }

    class _Ctx:
        class db:
            @staticmethod
            def one(*_a, **_k):
                return {"runs_24h": 0, "cost_24h": 0, "tokens_in_24h": 0, "tokens_out_24h": 0,
                        "done_24h": 0, "failed_24h": 0}

    scoped = state_view.restrict_to_projects(_Ctx(), data, {"TESTWP"})
    assert [item["key"] for item in scoped["slow_verifications"]] == ["TESTWP-85"]


def test_dashboard_panel_is_injected_into_the_console():
    page = console_template()
    assert 'data-block="slow-verifications"' in page
    assert "Slow verifications" in page or "AIFlowDashboardAttention" in page
    assert "dashboard_attention" in page or "slowVerificationsPanel" in page
