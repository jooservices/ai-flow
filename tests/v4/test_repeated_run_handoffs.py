"""Consecutive run diagnostics and coordinator handoffs (JAIF-354)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from conftest import add_ticket

from aiflow.v4.control import shared_accounts
from aiflow.v4.control.db import J
from aiflow.v4.control.features.incidents import repository, service
from aiflow.v4.control.features.fleet.heartbeat import _resource_sample
from aiflow.v4.control.features.work.events import RunFinished
from aiflow.v4.core.controlrepo import Account


def _run_event(run_id: str, step: str, outcome: str) -> RunFinished:
    return RunFinished(run_id=run_id, ticket_key="TESTWP-354", project="TESTWP", step=step, fence=1,
                       account="claude-01", provider="claude", model="opus", node="cc", outcome=outcome)


def _insert_run(ctx, step: str, outcome: str, ended_minutes_ago: int, *, summary: str = "worker timed out",
                details: dict | None = None, progress: dict | None = None) -> str:
    run_id = str(uuid4())
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=ended_minutes_ago)
    started_at = ended_at - timedelta(seconds=45)
    ctx.db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,error_kind,"
        "summary,details,progress,tokens_in,tokens_out,cost_usd,started_at,ended_at)"
        " VALUES (%s,'TESTWP-354','TESTWP',%s,'claude-01','claude','opus','cc',1,'finished',%s,'timeout',"
        "%s,%s,%s,11,7,0.25,%s,%s)",
        (run_id, step, outcome, summary, J(details or {"diagnostic_usage_reported": {
            "tokens_in": True, "tokens_out": True, "cost_usd": True}}), J(progress or {}), started_at, ended_at))
    ctx.db.run("INSERT INTO node_heartbeat_samples (node,heartbeat_at,metrics) VALUES ('cc',%s,%s)",
               (ended_at - timedelta(seconds=1), J({"cpu_pct": 62.5, "load1": 2.0, "cpus": 4,
                                                     "mem_used": 512, "mem_total": 1024})))
    return run_id


def _claim(ctx) -> bool:
    values = (str(uuid4()), "TESTWP-354", "TESTWP", "dev", "claude-01", "claude", "opus", "cc", 1, "test-hash")
    return shared_accounts.claim_run(ctx.db, Account("claude-01", "claude", "cc", parallel=2), values)


def test_consecutive_failures_stop_at_success_or_unlisted_outcome():
    from aiflow.v4.control.features.incidents.models import FailureRun

    now = datetime.now(timezone.utc)
    failures = [FailureRun(str(i), outcome, now, now, {}) for i, outcome in enumerate(
        ("timeout", "failed", "lost", "no_changes"), 1)]
    assert [run.outcome for run in service.consecutive_failures(failures)] == [
        "timeout", "failed", "lost", "no_changes"]
    assert service.consecutive_failures([failures[0], FailureRun("ok", "done", now, now, {}), failures[1]]) == [failures[0]]


def test_diagnostics_redact_secrets_and_mark_unreported_values_unavailable():
    from aiflow.v4.control.features.incidents.models import RunSnapshot

    now = datetime.now(timezone.utc)
    run = RunSnapshot("run-1", "TESTWP-354", "dev", "cc", "claude-01", "claude", "opus", now, now,
                      now, "finished", "failed", "timeout", "password=do-not-retain-this", {}, {}, 0, 0, 0,
                      None, {})
    record = service.diagnostic_record(run)
    assert "do-not-retain-this" not in str(record)
    assert record["tokens"] == record["cost_usd"] == "unavailable"
    assert record["node_heartbeat"]["load1"] == "unavailable"


def test_heartbeat_samples_only_finite_numeric_resource_measurements():
    sample = _resource_sample({"cpu_pct": 61.0, "load1": 1.5, "cpus": 8, "mem_used": 50,
                               "mem_total": 100, "hostname": "password=do-not-store", "mem_free": float("inf")})
    assert sample == {"cpu_pct": 61.0, "load1": 1.5, "cpus": 8, "mem_used": 50, "mem_total": 100}


def test_third_failure_opens_one_handoff_blocks_claims_and_resolution_resets_streak(ctx):
    add_ticket(ctx.db, "TESTWP-354")
    runs = []
    for step, outcome, age in (("dev", "timeout", 4), ("verify", "failed", 3), ("dev", "lost", 2)):
        run_id = _insert_run(ctx, step, outcome, age, summary="password=do-not-retain-this",
                             progress={"phase": "tests", "last": "pytest", "reported_at": "recent"})
        runs.append(run_id)
        service.record_run_outcome(ctx, _run_event(run_id, step, outcome))

    incident = ctx.db.one("SELECT id,title,detail FROM incidents WHERE kind='ticket_stuck' AND subject='TESTWP-354'")
    payload = service.handoff_payload(incident["detail"])
    assert payload["streak_count"] == 3 and [row["run_id"] for row in payload["runs"]] == runs
    assert "do-not-retain-this" not in incident["detail"]
    assert payload["notification"]["state"] == "missing_destination"
    assert payload["notification"]["configuration_gap"]
    assert all(ctx.db.one("SELECT details->'diagnostic' AS diagnostic FROM runs WHERE id=%s", (run_id,))["diagnostic"]
               for run_id in runs)
    assert len(payload["cause"]["evidence"]) == 3 and payload["cause"]["unknowns"]
    assert payload["runs"][-1]["node_heartbeat"]["memory_used_pct"] == 50.0
    assert payload["runs"][-1]["node_heartbeat"]["cpu_pct"] == 62.5
    assert not _claim(ctx)

    event_count = ctx.db.one("SELECT count(*) AS n FROM domain_events WHERE type='incident.opened'")["n"]
    service.record_run_outcome(ctx, _run_event(runs[-1], "dev", "lost"))
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='ticket_stuck' AND subject='TESTWP-354'")["n"] == 1
    assert ctx.db.one("SELECT count(*) AS n FROM domain_events WHERE type='incident.opened'")["n"] == event_count

    fourth = _insert_run(ctx, "verify", "timeout", 1)
    service.record_run_outcome(ctx, _run_event(fourth, "verify", "timeout"))
    updated = ctx.db.one("SELECT id,detail FROM incidents WHERE kind='ticket_stuck' AND subject='TESTWP-354'")
    updated_payload = service.handoff_payload(updated["detail"])
    assert updated["id"] == incident["id"] and updated_payload["streak_count"] == 4
    assert [row["run_id"] for row in updated_payload["runs"]] == [runs[1], runs[2], fourth]
    assert updated_payload["notification"]["state"] == payload["notification"]["state"]
    assert not _claim(ctx)

    ctx.db.run("UPDATE tickets SET labels=array_append(labels,'aiflow-hold') WHERE key='TESTWP-354'")
    ctx.db.run("INSERT INTO retry_holds (ticket_key,step,fails,run_ids) VALUES ('TESTWP-354','dev',3,%s)",
               ([runs[-1]],))
    result = service.resolve_and_requeue(ctx, incident["id"], "Coordinator", "Fixed the timeout by limiting test workers.")
    assert result == {"resolved": True, "ticket": "TESTWP-354", "status": "To Do"}
    assert ("remove", "TESTWP-354", "aiflow-hold") in ctx.jira.labels
    assert "aiflow-hold" not in ctx.db.one("SELECT labels FROM tickets WHERE key='TESTWP-354'")["labels"]
    assert ctx.db.one("SELECT released_at FROM retry_holds WHERE ticket_key='TESTWP-354'")["released_at"] is not None
    assert not repository.blocked_handoff_tickets(ctx.db)
    assert _claim(ctx)


def test_atomic_claim_guard_catches_three_failures_before_handoff_listener_runs(ctx):
    add_ticket(ctx.db, "TESTWP-354")
    for age in (4, 3, 2):
        _insert_run(ctx, "dev", "timeout", age)

    assert not repository.blocked_handoff_tickets(ctx.db)
    assert not _claim(ctx)
