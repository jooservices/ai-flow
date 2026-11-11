"""JAIF-618: report one warning after every eligible verifier has missed a verdict this round."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from conftest import add_ticket, join_node
from aiflow.v4.control import verify_no_verdict
from aiflow.v4.control.db import J
from aiflow.v4.control.loops import watchdog
from aiflow.v4.control.repositories.runs import verify_ticket_history

PR = "https://github.com/jooservices/test-aiflow-wp/pull/7"


def test_barred_requires_two_consecutive_no_verdict_markers():
    assert not verify_no_verdict.barred([])
    assert not verify_no_verdict.barred([True])
    assert not verify_no_verdict.barred([True, False])
    assert verify_no_verdict.barred([True, True])
    assert not verify_no_verdict.barred([False, True])


def test_default_candidate_probe_treats_http_exception_as_no_offer(monkeypatch):
    def denied(*_args, **_kwargs):
        raise HTTPException(status_code=503)

    monkeypatch.setattr(verify_no_verdict, "_work_deps",
                        lambda _ctx, _releases: SimpleNamespace(eligible=denied))
    offers = verify_no_verdict.ensure_offers(SimpleNamespace(), None)

    assert offers("claude-01", "cc") == []


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


def test_one_no_verdict_each_opens_one_warning_without_barring_accounts(client, ctx):
    _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    for account in ("codex-01", "claude-01"):
        _no_verdict(ctx, "TESTWP-50", account, minutes_ago=8)

    watchdog(ctx)
    watchdog(ctx)

    rows = _open_verify_incidents(ctx)
    assert len(rows) == 1 and rows[0]["subject"] == "TESTWP-50"
    assert "missing verdict" in rows[0]["title"]
    assert "at least one completed verification" in rows[0]["detail"]
    assert "current development round" in rows[0]["detail"]
    assert "two-result offer bar remains unchanged" in rows[0]["detail"]
    assert "claude-01" not in rows[0]["detail"] and "codex-01" not in rows[0]["detail"]
    assert set(verify_no_verdict.exhausted(ctx, {"key": "TESTWP-50", "labels": ["aiflow-ready"]})) == {
        "codex-01", "claude-01"}
    history = verify_ticket_history(ctx.db, ["TESTWP-50"])["TESTWP-50"].by_account
    assert all(not verify_no_verdict.barred(history[account]) for account in ("codex-01", "claude-01"))
    state = client.get("/v1/state").json()
    assert any(item["kind"] == "verify_no_verdict" and item["subject"] == "TESTWP-50"
               for item in state["incidents"])


def test_empty_or_ordinary_history_blocks_exhaustion_but_later_ordinary_result_does_not_erase_marker(client, ctx):
    _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    _no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=8)
    _no_verdict(ctx, "TESTWP-51", "claude-01", minutes_ago=7)
    watchdog(ctx)
    assert _open_verify_incidents(ctx) == []

    _insert_run(ctx, "TESTWP-50", "verify", "claude-01", "failed", details={"error": "ordinary"},
                minutes_ago=6)
    watchdog(ctx)
    assert _open_verify_incidents(ctx) == []

    _no_verdict(ctx, "TESTWP-50", "claude-01", minutes_ago=5)
    watchdog(ctx)
    assert len(_open_verify_incidents(ctx)) == 1
    _insert_run(ctx, "TESTWP-50", "verify", "codex-01", "failed", details={"error": "ordinary"},
                minutes_ago=4)
    watchdog(ctx)
    assert len(_open_verify_incidents(ctx)) == 1


def test_new_development_round_waits_for_capacity_then_closes_and_can_reopen(client, ctx):
    _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    for account in ("codex-01", "claude-01"):
        _no_verdict(ctx, "TESTWP-50", account, minutes_ago=8)
    watchdog(ctx)
    assert len(_open_verify_incidents(ctx)) == 1
    actual_offers = ctx.candidate_offers

    _insert_run(ctx, "TESTWP-50", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=4,
                details={"local_gate": {"status": "green"}})
    _insert_run(ctx, "TESTWP-50", "verify", "claude-01", "failed", details={"error": "ordinary"},
                minutes_ago=3)
    history = verify_ticket_history(ctx.db, ["TESTWP-50"])["TESTWP-50"].by_account
    assert history == {"claude-01": [False]}
    ctx.candidate_offers = lambda *_args, **_kwargs: []
    verify_no_verdict.check_incidents(ctx)
    assert len(_open_verify_incidents(ctx)) == 1

    ctx.candidate_offers = actual_offers
    verify_no_verdict.check_incidents(ctx)
    assert _open_verify_incidents(ctx) == []
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'")["n"] == 1

    for account in ("codex-01", "claude-01"):
        _no_verdict(ctx, "TESTWP-50", account, minutes_ago=3)
    verify_no_verdict.check_incidents(ctx)
    verify_no_verdict.check_incidents(ctx)
    assert len(_open_verify_incidents(ctx)) == 1
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'")["n"] == 2

    ctx.db.run("UPDATE tickets SET status='Done' WHERE key=%s", ("TESTWP-50",))
    verify_no_verdict.check_incidents(ctx)
    assert _open_verify_incidents(ctx) == []


def test_empty_and_http_exception_pools_never_open_and_preserve_open_incident(client, ctx):
    _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    verify_no_verdict.check_incidents(ctx)
    actual_offers = ctx.candidate_offers

    ctx.candidate_offers = lambda *_args, **_kwargs: []
    verify_no_verdict.check_incidents(ctx)
    assert _open_verify_incidents(ctx) == []

    def unavailable(*_args, **_kwargs):
        raise HTTPException(status_code=503)

    ctx.candidate_offers = unavailable
    verify_no_verdict.check_incidents(ctx)
    assert _open_verify_incidents(ctx) == []

    ctx.candidate_offers = actual_offers
    for account in ("codex-01", "claude-01"):
        _no_verdict(ctx, "TESTWP-50", account, minutes_ago=8)
    verify_no_verdict.check_incidents(ctx)
    assert len(_open_verify_incidents(ctx)) == 1

    ctx.candidate_offers = unavailable
    verify_no_verdict.check_incidents(ctx)
    assert len(_open_verify_incidents(ctx)) == 1
    ctx.candidate_offers = lambda *_args, **_kwargs: []
    verify_no_verdict.check_incidents(ctx)
    assert len(_open_verify_incidents(ctx)) == 1


def test_non_http_candidate_probe_errors_still_propagate(client, ctx):
    _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)

    def broken_probe(*_args, **_kwargs):
        raise RuntimeError("probe failed")

    ctx.candidate_offers = broken_probe
    with pytest.raises(RuntimeError, match="probe failed"):
        verify_no_verdict.check_incidents(ctx)
