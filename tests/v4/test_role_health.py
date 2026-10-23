"""Role health (JAIF-243): outcome scoring, switching a role off per AI account, probation and the owner's thresholds."""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import notify, role_health
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import J

SECRET = "test-console-role-secret-with-at-least-32-bytes"
EASY = "Fix the rounding.\nAcceptance criteria:\n- AC1: total is rounded\n- AC2: a unit test covers it"


def run(db, *, account="claude-01", step="dev", outcome="done", ago=60, ticket=None, kind="", summary="",
        cost=0.0, status="finished", details=None, pr_url=""):
    """A finished run that started `ago` minutes ago (the larger, the older)."""
    started = datetime.now(timezone.utc) - timedelta(minutes=ago)
    rid = str(uuid.uuid4())
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, node, fence, status, outcome,"
           " error_kind, summary, details, pr_url, started_at, ended_at, cost_usd)"
           " VALUES (%s,%s,'TESTWP',%s,%s,'claude','cc',1,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
           (rid, ticket or f"TESTWP-{uuid.uuid4().hex[:6]}", step, account, status, outcome, kind, summary,
            J(details or {}), pr_url, started, started + timedelta(minutes=5), cost))
    return rid


def failures(db, failed, ok, account="claude-01", step="dev", start=200):
    """`failed` failed runs and `ok` successes, oldest first, all with distinct tickets."""
    ago = start
    for outcome in ["failed"] * failed + ["merged" if step == "verify" else "done"] * ok:
        ago -= 1
        key = f"TESTWP-{uuid.uuid4().hex[:6]}"
        run(db, account=account, step=step, outcome=outcome, ago=ago, ticket=key)
        if step == "dev" and outcome == "done":  # accepted by verification
            run(db, account="codex-01", step="verify", outcome="merged", ago=ago - 0.5, ticket=key)


def states(db):
    return {(r["account_id"], r["role"]): r for r in db.all("SELECT * FROM role_health")}


def sign_in(client, ctx, role="owner", subject="owner@example.test"):
    ctx.settings.console_session_secret = SECRET
    client.cookies.set("aiflow4_session", issue_session(subject, role, 2_000_000_000, SECRET))


def cc_headers(client, ctx):
    r = join_node(client, "cc")
    ctx.db.run("UPDATE nodes SET status='online',last_heartbeat=now() WHERE name='cc'")
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail="", link="", recipient="", **_kw: sent.append((severity, title)) or [])
    return sent


@pytest.fixture(autouse=True)
def ready_role_pool(ctx):
    ctx.db.run("INSERT INTO nodes (name,status,last_heartbeat) VALUES ('cc','online',now()),('node1','online',now())"
               " ON CONFLICT (name) DO UPDATE SET status='online',last_heartbeat=now()")
    for account in ctx.repo().accounts:
        ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status) VALUES (%s,%s,%s,%s,%s,'idle')"
                   " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider,node=EXCLUDED.node,tier=EXCLUDED.tier,"
                   " roles=EXCLUDED.roles,status='idle'",
                   (account.id, account.provider, account.node, account.tier, role_health.account_roles(account)))


def ready_enabled_count(ctx, role):
    disabled = role_health.disabled_roles(ctx.db)
    online = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE status='online'")}
    statuses = {row["id"]: row["status"] for row in ctx.db.all("SELECT id,status FROM accounts")}
    return sum(1 for account in ctx.repo().accounts if role in role_health.account_roles(account)
               and not account.paused and statuses.get(account.id) in ("idle", "working", "probing")
               and online.intersection(account.machines) and (account.id, role) not in disabled)


# --- AC1: outcome scoring --------------------------------------------------------------------------------------
@pytest.mark.parametrize("run_, label, failure", [
    ({"step": "verify", "outcome": "merged"}, "merged", 0.0),
    ({"step": "dev", "outcome": "done", "decision": "merged"}, "finished", 0.0),
    ({"step": "dev", "outcome": "done", "decision": "human"}, "finished", 0.0),
    ({"step": "dev", "outcome": "done", "decision": "rework"}, "sent_back", 0.5),
    ({"step": "dev", "outcome": "failed", "decision": "rework", "pr_url": "https://example.test/pr/1"}, "failed", 1.0),
    ({"step": "dev", "outcome": "tool_missing", "pr_url": "https://example.test/pr/2", "details": {
        "missing_tools": ["php"], "verdict_reason": "MISSING-TOOL: php"}}, "tool_missing", 1.0),
    ({"step": "dev", "outcome": "done", "pr_url": "https://example.test/pr/3", "details": {
        "pr_url_source": "current"}}, "finished", 0.0),
    ({"step": "dev", "outcome": "no_changes"}, "no_changes", 0.0),
    ({"step": "dev", "outcome": "failed", "summary": "no changes: nothing to commit"}, "no_changes", 0.0),
    ({"step": "dev", "outcome": "tool_missing", "summary": "Nothing to change."}, "no_changes", 0.0),
    ({"step": "dev", "outcome": "blocked"}, "blocked", 1.0),
    ({"step": "dev", "outcome": "failed", "summary": "worker error"}, "failed", 1.0),
    ({"step": "dev", "outcome": "tool_missing", "details": {
        "missing_tools": ["php"], "verdict_reason": "MISSING-TOOL: php - check cannot run"}}, "tool_missing", 1.0),
    ({"step": "dev", "outcome": "tool_missing", "details": {
        "missing_tools": ["php"], "tool_missing_evidence": "command_not_found"}}, "excluded", None),
    ({"step": "dev", "outcome": "timeout"}, "stopped", 1.0),
    ({"step": "dev", "outcome": "lost"}, "stopped", 1.0),
    ({"step": "intake", "outcome": "refined"}, "finished", 0.0),
    ({"step": "triage", "outcome": "answered"}, "finished", 0.0),
    ({"step": "verify", "outcome": "rework"}, "finished", 0.0),
    ({"step": "dev", "outcome": "quota"}, "excluded", None),
    ({"step": "dev", "outcome": "failed", "error_kind": "quota"}, "excluded", None),
    ({"step": "verify", "outcome": "failed", "error_kind": "infra"}, "excluded", None),
    ({"step": "dev", "outcome": "failed", "error_kind": "network"}, "excluded", None),
])
def test_outcome_scoring(run_, label, failure):
    scored = role_health.classify({"error_kind": "", "summary": "", **run_})
    assert (scored.label, scored.failure) == (label, failure)


def test_a_finished_dev_run_waits_for_verification():
    assert role_health.classify({"step": "dev", "outcome": "done", "error_kind": "", "summary": ""}) is None


def test_a_failing_follow_up_run_with_inherited_pr_is_still_a_failure():
    scored = role_health.classify({"step": "dev", "outcome": "failed", "error_kind": "",
                                   "summary": "follow-up failed", "pr_url": "https://example.test/pr/1",
                                   "details": {"pr_url_source": "inherited"}})
    assert (scored.label, scored.failure) == ("failed", 1.0)


def test_steps_map_to_the_roles_of_the_account():
    assert [role_health.role_of(s) for s in ("dev", "verify", "triage", "master", "intake", "impact", "qa")] == [
        "dev", "verify", "master", "master", "intake_refiner", "intake_refiner", None]


def test_dev_run_is_decided_by_the_verify_run_that_follows_it(ctx):
    db = ctx.db
    run(db, outcome="done", ago=50, ticket="TESTWP-1")
    run(db, outcome="done", ago=40, ticket="TESTWP-2")
    run(db, outcome="done", ago=30, ticket="TESTWP-3")
    run(db, account="codex-01", step="verify", outcome="merged", ago=45, ticket="TESTWP-1")
    run(db, account="codex-01", step="verify", outcome="rework", ago=35, ticket="TESTWP-2")
    history = role_health._history(db, "claude-01")[("claude-01", "dev")]
    labels = {r["ticket_key"]: (s.label if s else "pending") for r, s in history}
    assert labels == {"TESTWP-1": "finished", "TESTWP-2": "sent_back", "TESTWP-3": "pending"}


# --- AC2: the failure score and switching a role off ----------------------------------------------------------
def test_score_needs_a_full_window_of_eligible_runs(ctx, alerts):
    failures(ctx.db, 9, 0)
    role_health.evaluate(ctx)
    assert states(ctx.db) == {}
    run(ctx.db, outcome="failed", ago=1)
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False


def test_threshold_is_strictly_above_sixty_percent(ctx, alerts):
    failures(ctx.db, 6, 4)  # exactly 60%
    role_health.evaluate(ctx)
    assert states(ctx.db) == {}
    ctx.db.run("DELETE FROM runs WHERE account_id='claude-01' AND outcome='done' AND started_at = ("
               "SELECT max(started_at) FROM runs WHERE account_id='claude-01' AND outcome='done')")
    run(ctx.db, outcome="failed", ago=1)  # 7 failed of the latest 10
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False


def _failure_window(db, account, *, prefix, outcome="failed", details=None, start=200):
    for index in range(10):
        ago = start - index
        run(db, account=account, outcome=outcome, ago=ago, ticket=f"TESTWP-{prefix}{index}",
            summary=f"{outcome} run", details=details)


def test_three_account_role_never_disables_below_two_and_all_bad_keeps_pool_on(ctx, alerts):
    db = ctx.db
    _failure_window(db, "claude-01", prefix="701")
    _failure_window(db, "codex-01", prefix="711")
    role_health.evaluate(ctx)
    assert ready_enabled_count(ctx, "dev") == 2

    _failure_window(db, "opencode-01", prefix="721")
    role_health.evaluate(ctx)
    assert ready_enabled_count(ctx, "dev") == 3
    incident = db.one("SELECT * FROM incidents WHERE kind='role_health' AND subject='dev' AND closed_at IS NULL")
    assert incident and "most frequent cause: failed" in incident["title"]
    assert db.one("SELECT count(*) AS n FROM incidents WHERE kind='role_health' AND closed_at IS NULL")["n"] == 1


def test_concurrent_role_health_evaluations_serialize_capacity_decisions(ctx, alerts, monkeypatch):
    _failure_window(ctx.db, "claude-01", prefix="731")
    _failure_window(ctx.db, "codex-01", prefix="741")

    original_states = role_health._states
    guard = threading.Lock()
    active = 0
    max_active = 0

    def tracked_states(db):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)  # widen the race window between the capacity snapshot and role updates
            return original_states(db)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(role_health, "_states", tracked_states)
    start = threading.Barrier(3)

    def evaluate_together():
        start.wait()
        role_health.evaluate(ctx)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(evaluate_together)
        second = pool.submit(evaluate_together)
        start.wait()
        first.result(timeout=30)
        second.result(timeout=30)

    assert max_active == 1
    assert ready_enabled_count(ctx, "dev") >= 2


def test_role_health_incident_lists_failure_kinds_and_tickets(ctx, alerts):
    db = ctx.db
    _failure_window(db, "claude-01", prefix="801", outcome="tool_missing", details={
        "missing_tools": ["php"], "verdict_reason": "MISSING-TOOL: php - check cannot run",
        "tool_missing_evidence": "explicit_marker"})
    _failure_window(db, "codex-01", prefix="811", outcome="timeout")
    _failure_window(db, "opencode-01", prefix="821")
    for index in range(5):
        db.run("UPDATE runs SET outcome='failed' WHERE ticket_key=%s", (f"TESTWP-801{index}",))
        db.run("UPDATE runs SET outcome='failed' WHERE ticket_key=%s", (f"TESTWP-811{index}",))
    # Two of opencode's ordinary failures are scored as rework rounds after verification sends them back.
    for index in range(2):
        key = f"TESTWP-821{index + 8}"
        db.run("UPDATE runs SET outcome='done' WHERE ticket_key=%s", (key,))
        run(db, account="codex-01", step="verify", outcome="rework", ago=180 - index,
            ticket=key)
    role_health.evaluate(ctx)
    incident = db.one("SELECT * FROM incidents WHERE kind='role_health' AND subject='dev' AND closed_at IS NULL")
    assert incident and "most frequent cause: failed" in incident["title"]
    assert "failed=18" in incident["detail"]
    assert "timeout=5" in incident["detail"]
    assert "tool_missing=5" in incident["detail"]
    assert "rework=2" in incident["detail"]
    assert "TESTWP-8010" in incident["detail"] and "Tickets involved:" in incident["detail"]


def test_fixed_run_history_restores_disabled_role_immediately(ctx, alerts):
    switch_off_dev(ctx)
    ctx.db.run("UPDATE runs SET outcome='merged' WHERE account_id='claude-01' AND step='dev' AND outcome='failed'")
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is True
    assert ctx.db.one("SELECT 1 AS x FROM incidents WHERE kind='role_health' AND subject='claude-01:dev' AND closed_at IS NOT NULL")


def test_legacy_tool_missing_false_positives_without_execution_evidence_restore_role(ctx, alerts):
    _failure_window(ctx.db, "claude-01", prefix="902", outcome="tool_missing", details={
        "missing_tools": ["php"], "tool_missing_evidence": "command_not_found",
        "verdict_reason": 'The summary mentions "bash: php: command not found" as an example.'})
    ctx.db.run("INSERT INTO nodes (name,status,last_heartbeat,tools) VALUES ('cc','online',now(),%s)"
               " ON CONFLICT (name) DO UPDATE SET tools=EXCLUDED.tools,last_heartbeat=now()",
               (J({"php": "missing"}),))
    role_health.switch_off(ctx, "claude-01", "dev", 1.0, 10,
                           role_health._history(ctx.db, "claude-01")[("claude-01", "dev")], 60)
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is True
    history = role_health._history(ctx.db, "claude-01")[("claude-01", "dev")]
    assert all(score is not None and score.failure is None for _, score in history)


def test_installing_recorded_missing_tool_restores_disabled_role(ctx, alerts):
    _failure_window(ctx.db, "claude-01", prefix="901", outcome="tool_missing",
                    details={"missing_tools": ["php"], "verdict_reason": "MISSING-TOOL: php - check cannot run",
                             "tool_missing_evidence": "explicit_marker"})
    ctx.db.run("INSERT INTO nodes (name,status,last_heartbeat,tools) VALUES ('cc','online',now(),%s)"
               " ON CONFLICT (name) DO UPDATE SET status='online',last_heartbeat=now(),tools=EXCLUDED.tools",
               (J({"php": "missing"}),))
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False
    ctx.db.run("UPDATE nodes SET tools=%s WHERE name='cc'", (J({"php": "PHP 8.3"}),))
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is True


def test_only_the_latest_ten_eligible_runs_count(ctx, alerts):
    failures(ctx.db, 10, 0, start=400)          # old failures
    failures(ctx.db, 0, 10, start=100)          # ten recent successes push them out of the window
    role_health.evaluate(ctx)
    assert states(ctx.db) == {}


def test_sent_back_runs_count_half(ctx, alerts):
    for i in range(10):  # 10 sent-back dev runs: score 50%
        key = f"TESTWP-{i + 1}"
        run(ctx.db, outcome="done", ago=100 - i * 2, ticket=key)
        run(ctx.db, account="codex-01", step="verify", outcome="rework", ago=99 - i * 2, ticket=key)
    role_health.evaluate(ctx)
    assert states(ctx.db) == {}
    score, used = role_health.failure_score(role_health._history(ctx.db, "claude-01")[("claude-01", "dev")], 10)
    assert (score, used) == (0.5, 10)
    for i in range(3):  # three failures replace three sent-back runs: 3 + 3.5 = 6.5 of 10
        run(ctx.db, outcome="failed", ago=10 - i)
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False


def test_quota_and_machine_runs_are_not_scored(ctx, alerts):
    failures(ctx.db, 9, 0)
    for i, (outcome, kind) in enumerate([("quota", "quota"), ("failed", "infra"), ("failed", "network"),
                                        ("failed", "auth"), ("failed", "tool")]):
        run(ctx.db, outcome=outcome, kind=kind, ago=20 - i)
    role_health.evaluate(ctx)
    assert states(ctx.db) == {}          # still only 9 eligible runs
    run(ctx.db, outcome="timeout", ago=1)  # a run stopped by the system is the 10th
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False


def test_switching_a_role_off_keeps_the_other_roles_alerts_and_notes_it(client, ctx, alerts):
    failures(ctx.db, 8, 2)
    failures(ctx.db, 1, 9, step="verify", start=300)
    role_health.evaluate(ctx)
    row = states(ctx.db)[("claude-01", "dev")]
    assert row["enabled"] is False and "failure score 80%" in row["note"] and "other roles stay on" in row["note"]
    assert ("claude-01", "verify") not in states(ctx.db)  # the account's other roles are untouched
    incident = ctx.db.one("SELECT * FROM incidents WHERE kind='role_health' AND closed_at IS NULL")
    assert incident["subject"] == "claude-01:dev" and incident["severity"] == "warning"
    assert alerts and "claude-01" in alerts[0][1]
    assert ctx.db.one("SELECT 1 AS x FROM events WHERE type='role.disabled' AND account='claude-01'")
    role_health.evaluate(ctx)  # no second alert while it stays off
    assert len(alerts) == 1

    cc = cc_headers(client, ctx)
    add_ticket(ctx.db, "TESTWP-50")
    add_ticket(ctx.db, "TESTWP-51", status="In Verify", labels=())
    run(ctx.db, account="codex-01", step="dev", outcome="done", ago=5, ticket="TESTWP-51")
    ctx.db.run("UPDATE runs SET pr_url='https://github.com/x/y/pull/1' WHERE ticket_key='TESTWP-51'")
    steps = {(t["key"], t["step"]) for t in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                                         headers=cc).json()["tickets"]}
    assert steps == {("TESTWP-51", "verify")}      # verify still works, the dev ticket is not offered
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "claude-01"},
                       headers=cc).status_code == 409
    state = client.get("/v1/state").json()
    assert {r["role"]: r["mode"] for r in state["role_health"]["roles"] if r["account"] == "claude-01"} == {
        "dev": "probation", "verify": "on", "master": "on"}


def test_role_result_triggers_the_switch_for_a_real_failure(client, ctx, alerts):
    cc = cc_headers(client, ctx)
    failures(ctx.db, 9, 0)
    add_ticket(ctx.db, "TESTWP-60")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-60", "account": "claude-01"}, headers=cc).json()
    client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "failed",
                                                       "summary": "worker error: checkout failed"}, headers=cc)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False


def test_role_health_keeps_two_ready_accounts_enabled_when_a_peer_is_unavailable(client, ctx, alerts):
    from aiflow.v4.control.loops import watchdog
    ctx.db.run("UPDATE accounts SET status='idle'")
    ctx.db.run("UPDATE accounts SET status='error' WHERE id='opencode-01'")
    ctx.db.run("INSERT INTO nodes (name, status, last_heartbeat) VALUES ('cc','online',now()),('node1','online',now())"
               " ON CONFLICT (name) DO UPDATE SET status='online', last_heartbeat=now()")
    watchdog(ctx)
    assert not ctx.db.one("SELECT 1 AS x FROM incidents WHERE kind='role_coverage' AND subject='dev' AND closed_at IS NULL")
    failures(ctx.db, 10, 0)
    watchdog(ctx)
    assert not ctx.db.one("SELECT 1 AS x FROM incidents WHERE kind='role_coverage' AND subject='dev' AND closed_at IS NULL")
    assert ready_enabled_count(ctx, "dev") == 2


# --- AC3: probation -------------------------------------------------------------------------------------------
def switch_off_dev(ctx):
    failures(ctx.db, 10, 0)
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False
    ctx.db.run("UPDATE role_health SET disabled_at = now() - interval '10 days'")  # probation rounds below follow it


def easy(db, key, **kw):
    add_ticket(db, key)
    db.run("UPDATE tickets SET type=%s, description=%s, labels=%s WHERE key=%s",
           (kw.get("type", "Bug"), kw.get("description", EASY), kw.get("labels", ["aiflow-ready"]), key))


@pytest.mark.parametrize("fields", [
    {"type": "Bug"}, {"type": "Chore"}], ids=["bug", "chore"])
def test_easy_tickets_are_small_bugs_or_chores_with_a_unit_test_and_no_ui(fields):
    assert role_health.probation_ticket({**fields, "description": EASY, "labels": [], "summary": "Fix rounding"})


@pytest.mark.parametrize("ticket", [
    {"type": "Story", "description": EASY},
    {"type": "Bug", "description": "Fix it.\n- AC1: it works"},
    {"type": "Bug", "description": EASY + "\n- AC3: the settings page shows it"},
    {"type": "Bug", "description": EASY, "labels": ["ui"]},
    {"type": "Bug", "description": EASY + "x" * 2500},
    {"type": "Bug", "description": EASY, "summary": "Button misaligned"},
])
def test_other_tickets_are_not_easy(ticket):
    assert not role_health.probation_ticket({"labels": [], "summary": "Fix rounding", **ticket})


def test_a_switched_off_role_can_probe_again_without_a_day_long_wait(client, ctx, alerts):
    cc = cc_headers(client, ctx)
    switch_off_dev(ctx)
    add_ticket(ctx.db, "TESTWP-70")                       # not easy (no unit test, Task)
    easy(ctx.db, "TESTWP-71")
    easy(ctx.db, "TESTWP-72", type="Chore")
    easy(ctx.db, "TESTWP-73", labels=["aiflow-ready", "ui"])
    offered = [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]]
    assert sorted(offered) == ["TESTWP-71", "TESTWP-72"]

    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-71", "account": "claude-01"}, headers=cc).json()
    assert claimed["claimed"]
    probation = ctx.db.one("SELECT * FROM role_probation")
    assert (probation["account_id"], probation["role"], probation["ticket_key"], probation["result"]) == (
        "claude-01", "dev", "TESTWP-71", "")
    assert str(probation["run_id"]) == claimed["run_id"]
    # A failed probe frees the slot immediately; the next probe need not wait until tomorrow.
    client.post(f"/v1/runs/{claimed['run_id']}/result", json={"fence": claimed["fence"], "outcome": "failed"}, headers=cc)
    offered = [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]]
    assert "TESTWP-72" in offered
    next_claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-72", "account": "claude-01"}, headers=cc).json()
    assert next_claim["claimed"]


def probation_round(ctx, key, outcome, day_offset):
    """One probation ticket developed by claude-01 and judged by codex-01 (`outcome` of the verify run)."""
    started = datetime.now(timezone.utc) - timedelta(days=day_offset)
    rid = run(ctx.db, outcome="done", ago=day_offset * 1440 + 30, ticket=key)
    ctx.db.run("INSERT INTO role_probation (account_id, role, ticket_key, run_id, assigned_at) VALUES"
               " ('claude-01','dev',%s,%s,%s)", (key, rid, started - timedelta(minutes=30)))
    run(ctx.db, account="codex-01", step="verify", outcome=outcome, ago=day_offset * 1440 + 10, ticket=key)


def test_two_successful_probation_tickets_in_a_row_bring_the_role_back(ctx, alerts):
    switch_off_dev(ctx)
    probation_round(ctx, "TESTWP-80", "merged", 0)
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False        # one success is not enough
    assert ctx.db.one("SELECT count(*) AS n FROM role_probation WHERE result='success'")["n"] == 1
    probation_round(ctx, "TESTWP-81", "human", 0)
    role_health.evaluate(ctx)
    row = states(ctx.db)[("claude-01", "dev")]
    assert row["enabled"] is True and row["scoring_from"] is not None
    assert ctx.db.one("SELECT 1 AS x FROM incidents WHERE kind='role_health' AND closed_at IS NOT NULL")
    assert ctx.db.one("SELECT 1 AS x FROM events WHERE type='role.enabled'")
    role_health.evaluate(ctx)  # the old failures no longer count: the role stays on
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is True


def test_a_failed_probation_ticket_breaks_the_streak(ctx, alerts):
    switch_off_dev(ctx)
    probation_round(ctx, "TESTWP-82", "merged", 2)
    probation_round(ctx, "TESTWP-83", "rework", 1)   # sent back is not a success
    probation_round(ctx, "TESTWP-84", "merged", 0)
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False
    assert [r["result"] for r in ctx.db.all("SELECT result FROM role_probation ORDER BY assigned_at")] == [
        "success", "failure", "success"]


def test_a_quota_stopped_probation_run_gives_the_slot_back(client, ctx, alerts):
    cc = cc_headers(client, ctx)
    switch_off_dev(ctx)
    easy(ctx.db, "TESTWP-90")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-90", "account": "claude-01"}, headers=cc).json()
    client.post(f"/v1/runs/{claimed['run_id']}/result", json={"fence": claimed["fence"], "outcome": "quota",
                                                              "error_kind": "quota"}, headers=cc)
    assert ctx.db.one("SELECT count(*) AS n FROM role_probation")["n"] == 0
    assert role_health.probation_slot_open(ctx.db, "claude-01", "dev")


# --- AC4: displayed metrics -----------------------------------------------------------------------------------
def test_metrics_per_account_and_role(client, ctx, alerts):
    db = ctx.db
    # claude-01 dev: 2 merged (cost 1.00 + 3.00), 1 sent back (2.00), 1 failed (4.00): 3 scored of... all 4 scored
    run(db, outcome="done", ago=40, ticket="TESTWP-1", cost=1.0)
    run(db, outcome="done", ago=30, ticket="TESTWP-2", cost=3.0)
    run(db, outcome="done", ago=20, ticket="TESTWP-3", cost=2.0)
    run(db, outcome="failed", ago=10, ticket="TESTWP-4", cost=4.0)
    run(db, outcome="quota", kind="quota", ago=5, ticket="TESTWP-5", cost=0.0)   # not scored
    run(db, account="codex-01", step="verify", outcome="merged", ago=35, ticket="TESTWP-1", cost=0.5)
    run(db, account="codex-01", step="verify", outcome="merged", ago=25, ticket="TESTWP-2", cost=0.5)
    run(db, account="codex-01", step="verify", outcome="rework", ago=15, ticket="TESTWP-3", cost=0.5)
    rows = {(r["account"], r["role"]): r for r in client.get("/v1/state").json()["role_health"]["roles"]}
    dev = rows[("claude-01", "dev")]
    assert dev["scored_runs"] == 4 and dev["success_rate"] == 62.5      # (0 + 0 + 0.5 + 1) / 4 failure score
    assert dev["finished_tickets"] == 2 and dev["cost_per_finished_ticket"] == 5.0   # (1 + 3 + 2 + 4) / 2
    assert dev["rework_rounds"] == 1 and dev["enabled"] is True and dev["mode"] == "on"
    verify = rows[("codex-01", "verify")]
    assert verify["success_rate"] == 100.0 and verify["rework_rounds"] == 1
    assert verify["finished_tickets"] == 3 and verify["cost_per_finished_ticket"] == 0.5
    assert rows[("claude-01", "master")]["success_rate"] is None
    assert client.get("/v1/role-health").json()["roles"] == client.get("/v1/state").json()["role_health"]["roles"]


def test_console_shows_the_role_health_panel_and_form():
    page = (Path(role_health.__file__).parent / "ui" / "console.html").read_text()
    for marker in ("AI quality by role", "roleHealthPanel", "success_rate", "cost_per_finished_ticket", "rework_rounds",
                   "id=\"rh-window\"", "id=\"rh-percent\"", "/v1/role-health/settings", "Switch the role off above",
                   "Failure counts and involved tickets appear above", "white-space:pre-wrap"):
        assert marker in page


# --- AC5: editable thresholds ---------------------------------------------------------------------------------
def test_defaults_are_ten_runs_and_sixty_percent(client):
    settings = client.get("/v1/role-health").json()["settings"]
    assert (settings["window_runs"], settings["failure_percent"]) == (10, 60)


def test_owner_edits_thresholds_and_they_apply(client, ctx, alerts):
    failures(ctx.db, 5, 5)                    # 50%: fine with the defaults
    role_health.evaluate(ctx)
    assert states(ctx.db) == {}
    sign_in(client, ctx, "owner")
    r = client.put("/v1/role-health/settings", json={"window_runs": 10, "failure_percent": 40})
    assert r.status_code == 200 and r.json()["settings"]["failure_percent"] == 40
    assert r.json()["settings"]["updated_by"] == "owner@example.test"
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False        # re-scored with the new threshold
    audit = ctx.db.one("SELECT * FROM write_audit WHERE action='role_health_settings' ORDER BY id DESC")
    assert audit["accepted"] and audit["actor"] == "owner@example.test"
    assert client.get("/v1/state").json()["role_health"]["settings"]["window_runs"] == 10


def test_a_smaller_window_scores_fewer_runs(client, ctx, alerts):
    failures(ctx.db, 4, 0, start=300)
    failures(ctx.db, 0, 6, start=100)
    sign_in(client, ctx, "owner")
    client.put("/v1/role-health/settings", json={"window_runs": 4, "failure_percent": 60})
    assert states(ctx.db) == {}               # latest 4 are successes
    failures(ctx.db, 4, 0, start=50)
    role_health.evaluate(ctx)
    assert states(ctx.db)[("claude-01", "dev")]["enabled"] is False


@pytest.mark.parametrize("body", [
    {"window_runs": 0, "failure_percent": 60}, {"window_runs": 101, "failure_percent": 60},
    {"window_runs": 10, "failure_percent": -1}, {"window_runs": 10, "failure_percent": 100},
    {"window_runs": 10},
])
def test_invalid_thresholds_are_refused(client, ctx, body):
    sign_in(client, ctx, "owner")
    assert client.put("/v1/role-health/settings", json=body).status_code == 422
    assert client.get("/v1/role-health").json()["settings"]["failure_percent"] == 60


def test_only_an_owner_can_edit_thresholds(client, ctx):
    body = {"window_runs": 5, "failure_percent": 50}
    assert client.put("/v1/role-health/settings", json=body).status_code in (401, 503)
    sign_in(client, ctx, "lead", "lead@example.test")
    assert client.put("/v1/role-health/settings", json=body).status_code == 403
    assert client.get("/v1/role-health").json()["settings"]["window_runs"] == 10
