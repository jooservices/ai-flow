"""Characterization and AC6: one verify_no_verdict incident when every eligible account is barred."""
import uuid
from datetime import datetime, timedelta, timezone

from conftest import add_ticket, join_node
from aiflow.v4.control import verify_no_verdict
from aiflow.v4.control.db import J
from aiflow.v4.control.loops import watchdog

PR = "https://github.com/jooservices/test-aiflow-wp/pull/7"


def test_barred_requires_two_consecutive_no_verdict_markers():
    assert not verify_no_verdict.barred([])
    assert not verify_no_verdict.barred([True])
    assert not verify_no_verdict.barred([True, False])
    assert verify_no_verdict.barred([True, True])
    assert not verify_no_verdict.barred([False, True])


def _join(client, name):
    joined = join_node(client, name)
    assert joined.status_code == 200
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    assert client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": 2}, headers=headers).status_code == 200
    return headers


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


def _in_verify(ctx, key="TESTWP-50"):
    add_ticket(ctx.db, key, "In Verify")
    _insert_run(ctx, key, "dev", "opencode-01", "done", pr_url=PR, minutes_ago=60,
                details={"local_gate": {"status": "green"}})


def _no_verdict(ctx, key, account, minutes_ago):
    _insert_run(ctx, key, "verify", account, "failed", details={"no_verdict": True},
                minutes_ago=minutes_ago, summary="no verdict (missing VERDICT line): x")


def _open_verify_incidents(ctx):
    return ctx.db.all("SELECT subject, title, detail FROM incidents WHERE kind='verify_no_verdict'"
                      " AND closed_at IS NULL")


def test_one_no_verdict_each_does_not_raise_verify_no_verdict_incident(client, ctx):
    """Characterization: a single miss per account is not exhaustion (unchanged before and after AC6)."""
    _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    for account in ("codex-01", "claude-01"):
        _no_verdict(ctx, "TESTWP-50", account, minutes_ago=8)
    watchdog(ctx)
    assert _open_verify_incidents(ctx) == []


def test_failure_on_all_eligible_accounts_raises_one_incident_and_the_dashboard_lists_it(client, ctx):
    """AC6 (JAIF-443): every eligible account barred → one incident; repeated checks do not add another."""
    _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    assert not verify_no_verdict.exhausted(ctx, {"key": "TESTWP-50", "labels": ["aiflow-ready"]})
    for account in ("codex-01", "claude-01"):
        _no_verdict(ctx, "TESTWP-50", account, minutes_ago=8)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'")["n"] == 0
    for account in ("codex-01", "claude-01"):
        _no_verdict(ctx, "TESTWP-50", account, minutes_ago=2)
    watchdog(ctx)
    watchdog(ctx)
    rows = _open_verify_incidents(ctx)
    assert len(rows) == 1 and rows[0]["subject"] == "TESTWP-50"
    assert "TESTWP-50" in rows[0]["title"] and "without a VERDICT line" in rows[0]["detail"]
    assert "claude-01" not in rows[0]["detail"] and "codex-01" not in rows[0]["detail"]
    # a newer development run does not reset ticket-wide account history or close the incident
    _insert_run(ctx, "TESTWP-50", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=0,
                details={"local_gate": {"status": "green"}})
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'"
                      " AND closed_at IS NULL")["n"] == 1
