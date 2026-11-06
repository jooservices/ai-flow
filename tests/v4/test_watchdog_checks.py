"""Characterization of the watchdog checks as they behave before the file split.

The tests call the check functions on a scripted database. They do not need PostgreSQL or Redis.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aiflow.v4.control import rollout, run_inbox, system_check, tooling
from aiflow.v4.control import environment as environment_reports
from aiflow.v4.control import role_health
from aiflow.v4.control.watchdog import (
    account_cooldowns,
    lost_leases_and_run_clocks,
    node_heartbeats,
    node_services,
    node_tools,
    nodes_without_accounts,
    orphaned_tickets,
    quota_forecasts,
    quota_windows,
    retry_and_deadline_passes,
    role_coverage,
    run,
    run_flow_and_quota_checks,
    run_machine_and_orphan_checks,
    stuck_tickets,
    tool_and_environment_waits,
    version_skew,
)

def ago(**delta):
    return datetime.now(timezone.utc) - timedelta(**delta)


class ScriptedDB:
    """Records SQL and answers the queries a check actually branches on."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._all = []
        self._one = []
        self.fail_all = None

    def all_when(self, needle: str, rows: list):
        self._all.append((needle, rows))
        return self

    def one_when(self, needle: str, row):
        self._one.append((needle, row))
        return self

    def all(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append(("all", flat, params))
        if self.fail_all and self.fail_all(flat):
            raise RuntimeError("heartbeat store down")
        for needle, rows in self._all:
            if needle in flat:
                return rows
        return []

    def one(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append(("one", flat, params))
        for needle, row in self._one:
            if needle in flat:
                return row
        return None

    def run(self, sql, params=None):
        self.calls.append(("run", " ".join(sql.split()), params))

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def _tx():
            yield self

        return _tx()


class Leases:
    def __init__(self, holders=None, cooling=()):
        self.holders = dict(holders or {})
        self._cooling = set(cooling)

    def holder(self, key):
        return self.holders.get(key)

    def cooling(self, account_id):
        return account_id in self._cooling

    def held_keys(self):
        return set(self.holders)


class Repo:
    def __init__(self):
        self.projects = {}
        self.accounts = []
        self.nodes = {}
        self.versions = {}
        self.inventory_in_db = True
        self.policy = {}

    def projects_for_node(self, name, projects, dynamic):
        return list(projects)

    def provider_tools_for_node(self, name):
        return []


class Events:
    def __init__(self):
        self.emitted = []

    def emit(self, type, **fields):
        self.emitted.append((type, fields))
        return {}


class Jira:
    def __init__(self, status="In Progress", fail=False):
        self._status = status
        self.fail = fail
        self.transitions = []
        self.comments = []
        self.status_calls = []

    def status(self, key):
        self.status_calls.append(key)
        return self._status

    def transition_to(self, key, status):
        if self.fail:
            raise RuntimeError("jira down")
        self.transitions.append((key, status))

    def comment(self, key, text):
        self.comments.append((key, text))


def make_ctx(db, repo=None, jira=None, leases=None):
    events = Events()
    ctx = SimpleNamespace(
        db=db, events=events, leases=leases or Leases(), jira=jira,
        repo=lambda: repo or Repo(),
        settings=SimpleNamespace(license_public_key="", maintenance_project="JAIF"),
        event_registry=None,
    )
    return ctx, events


def incident_inserts(db):
    return [call for call in db.calls if call[0] == "one" and call[1].startswith("INSERT INTO incidents")]


def incident_closes(db):
    return [call for call in db.calls if call[0] == "one" and call[1].startswith("UPDATE incidents SET closed_at")]


def sql_text(db):
    return [call[1] for call in db.calls]


def test_checks_run_in_the_existing_order():
    db = ScriptedDB()
    ctx, _events = make_ctx(db, jira=Jira())

    run(ctx)

    text = "\n".join(sql_text(db))
    anchors = [
        "last_heartbeat FROM nodes",
        "release_version FROM nodes",
        "tools, projects, dynamic FROM nodes",
        "metrics, projects, dynamic FROM nodes",
        "FROM tool_waits",
        "SELECT name, status FROM nodes",
        "interval '10 minutes'",
        "fails FROM streak",
        "FROM runs WHERE status='running'",
        "status='cooldown'",
        "status = 'online'",
        "quota ? 'windows'",
        "quota_observed_at",
    ]
    positions = [text.index(anchor) for anchor in anchors]
    assert positions == sorted(positions)


def test_a_failing_check_does_not_stop_later_checks(caplog):
    db = ScriptedDB()
    db.fail_all = lambda sql: "last_heartbeat" in sql
    ctx, _events = make_ctx(db)

    with caplog.at_level(logging.ERROR, logger="aiflow4.loops"):
        run(ctx)

    assert "watchdog check node_heartbeats failed" in caplog.text
    assert any("quota_observed_at" in sql for sql in sql_text(db))


def test_the_loop_runs_all_fourteen_checks_in_order_and_continues_after_one_raises(monkeypatch, caplog):
    import aiflow.v4.control.watchdog as watchdog

    names = [check.__name__ for check in watchdog.CHECKS]
    assert names == [
        "node_heartbeats",
        "version_skew",
        "node_tools",
        "node_services",
        "tool_and_environment_waits",
        "nodes_without_accounts",
        "orphaned_tickets",
        "stuck_tickets",
        "retry_and_deadline_passes",
        "lost_leases_and_run_clocks",
        "account_cooldowns",
        "role_coverage",
        "quota_windows",
        "quota_forecasts",
    ]
    order = []
    failing = "node_services"

    def tracking(name):
        def check(ctx):
            order.append(name)
            if name == failing:
                raise RuntimeError("check failed")
        check.__name__ = name
        return check

    monkeypatch.setattr(watchdog, "CHECKS", tuple(tracking(name) for name in names))

    with caplog.at_level(logging.ERROR, logger="aiflow4.loops"):
        watchdog.run(object())

    assert order == names
    assert "watchdog check node_services failed" in caplog.text


def test_run_calls_the_machine_group_before_the_flow_group(monkeypatch):
    order = []
    import aiflow.v4.control.watchdog as watchdog

    monkeypatch.setattr(watchdog, "run_machine_and_orphan_checks", lambda ctx: order.append("machines"))
    monkeypatch.setattr(watchdog, "run_flow_and_quota_checks", lambda ctx: order.append("flow"))

    watchdog.run(object())

    assert order == ["machines", "flow"]


def test_machine_checks_log_a_failure_and_still_reach_orphaned_tickets(caplog):
    db = ScriptedDB()
    db.fail_all = lambda sql: "last_heartbeat" in sql
    ctx, _events = make_ctx(db, jira=Jira())

    with caplog.at_level(logging.ERROR, logger="aiflow4.loops"):
        run_machine_and_orphan_checks(ctx)

    assert "watchdog check node_heartbeats failed" in caplog.text
    assert any("interval '10 minutes'" in sql for sql in sql_text(db))


def test_flow_checks_reach_the_quota_forecast_after_the_stuck_tickets():
    db = ScriptedDB()
    ctx, _events = make_ctx(db)

    run_flow_and_quota_checks(ctx)

    text = "\n".join(sql_text(db))
    assert text.index("fails FROM streak") < text.index("quota_observed_at")


def test_a_check_itself_still_raises_when_its_query_fails():
    db = ScriptedDB()
    db.fail_all = lambda sql: True
    ctx, _events = make_ctx(db)

    with pytest.raises(RuntimeError, match="heartbeat store down"):
        node_heartbeats(ctx)


def test_node_heartbeats_marks_a_silent_online_node_offline(monkeypatch):
    refreshed = []
    monkeypatch.setattr(system_check, "refresh_node", lambda ctx, name, trigger: refreshed.append((name, trigger)))
    db = ScriptedDB().all_when(
        "last_heartbeat FROM nodes",
        [{"name": "n1", "status": "online", "last_heartbeat": ago(seconds=600)}],
    )
    ctx, events = make_ctx(db)

    node_heartbeats(ctx)

    assert ("run", "UPDATE nodes SET status='offline' WHERE name=%s", ("n1",)) in db.calls
    assert events.emitted[0][0] == "node.offline"
    assert events.emitted[0][1]["node"] == "n1"
    assert events.emitted[0][1]["reason"].startswith("no heartbeat for 6")
    assert incident_inserts(db)[0][2][:2] == ("node_down", "n1")
    assert refreshed == [("n1", "heartbeat_lost")]


def test_node_heartbeats_closes_the_incident_when_the_heartbeat_is_fresh():
    db = ScriptedDB().all_when(
        "last_heartbeat FROM nodes",
        [{"name": "n1", "status": "online", "last_heartbeat": ago(seconds=10)}],
    )
    ctx, events = make_ctx(db)

    node_heartbeats(ctx)

    assert events.emitted == []
    assert incident_closes(db)[0][2][:2] == ("node_down", "n1")
    assert not any(call[0] == "run" and "nodes" in call[1] for call in db.calls)


def test_node_heartbeats_leaves_an_offline_node_alone():
    db = ScriptedDB().all_when(
        "last_heartbeat FROM nodes",
        [{"name": "n1", "status": "offline", "last_heartbeat": None}],
    )
    ctx, events = make_ctx(db)

    node_heartbeats(ctx)

    assert events.emitted == []
    assert incident_inserts(db) == []
    assert incident_closes(db) == []


def test_version_skew_asks_the_rollout_watch(monkeypatch):
    seen = []
    monkeypatch.setattr(rollout, "watch_skew", lambda ctx, releases: seen.append((ctx, releases)))
    ctx, _events = make_ctx(ScriptedDB())

    version_skew(ctx)

    from aiflow.v4.control import api
    assert seen[0][0] is ctx
    assert seen[0][1] is api.RELEASES


def test_node_tools_opens_an_incident_for_a_missing_project_tool():
    repo = Repo()
    repo.projects["P"] = SimpleNamespace(tools=["composer"], stacks=())
    db = ScriptedDB().all_when(
        "tools, projects, dynamic FROM nodes",
        [{"name": "n1", "status": "online", "tools": {}, "projects": ["P"], "dynamic": False}],
    )
    ctx, _events = make_ctx(db, repo=repo)

    node_tools(ctx)

    opened = incident_inserts(db)
    assert opened[0][2][0] == "node_tools"
    assert opened[0][2][1] == "n1:P"
    assert "composer" in opened[0][2][4]


def test_node_tools_keeps_the_incident_while_the_node_is_offline():
    repo = Repo()
    repo.projects["P"] = SimpleNamespace(tools=["composer"], stacks=())
    db = ScriptedDB().all_when(
        "tools, projects, dynamic FROM nodes",
        [{"name": "n1", "status": "offline", "tools": {}, "projects": ["P"], "dynamic": False}],
    ).all_when("kind='node_tools'", [{"subject": "n1:P"}])
    ctx, _events = make_ctx(db, repo=repo)

    node_tools(ctx)

    assert incident_inserts(db) == []
    assert incident_closes(db) == []


def test_node_services_opens_an_incident_for_an_unhealthy_service():
    repo = Repo()
    repo.projects["P"] = SimpleNamespace(required_services=["postgres"])
    db = ScriptedDB().all_when(
        "metrics, projects, dynamic FROM nodes",
        [{"name": "n1", "status": "online", "metrics": {"services": {}}, "projects": ["P"], "dynamic": False}],
    )
    ctx, _events = make_ctx(db, repo=repo)

    node_services(ctx)

    opened = incident_inserts(db)[0][2]
    assert opened[0] == "node_services"
    assert opened[1] == "n1:P"
    assert "postgres" in opened[4]


def test_tool_and_environment_waits_call_the_existing_passes_in_order(monkeypatch):
    order = []
    monkeypatch.setattr(tooling, "declare_undeclared_services", lambda ctx: order.append("declare"))
    monkeypatch.setattr(tooling, "sweep", lambda ctx: order.append("sweep"))
    monkeypatch.setattr(tooling, "check_stalled_waits", lambda ctx: order.append("stalled"))
    monkeypatch.setattr(environment_reports, "sweep", lambda ctx: order.append("environment"))
    monkeypatch.setattr(environment_reports, "scan_if_due", lambda ctx: order.append("scan"))
    ctx, _events = make_ctx(ScriptedDB())

    tool_and_environment_waits(ctx)

    assert order == ["declare", "sweep", "stalled", "environment", "scan"]


def test_nodes_without_accounts_opens_for_a_bare_node_and_closes_for_an_assigned_one():
    repo = Repo()
    repo.inventory_in_db = False
    repo.accounts = [SimpleNamespace(machines=["assigned"])]
    db = ScriptedDB().all_when(
        "SELECT name, status FROM nodes",
        [{"name": "assigned", "status": "online"}, {"name": "bare", "status": "online"}],
    )
    ctx, _events = make_ctx(db, repo=repo)

    nodes_without_accounts(ctx)

    opened = incident_inserts(db)
    assert len(opened) == 1
    assert opened[0][2][0] == "node_no_ai"
    assert opened[0][2][1] == "bare"
    assert "not described in aiflow-control/nodes/" in opened[0][2][4]
    assert incident_closes(db)[0][2][:2] == ("node_no_ai", "assigned")


def test_orphaned_tickets_does_nothing_without_jira():
    db = ScriptedDB()
    ctx, _events = make_ctx(db, jira=None)

    orphaned_tickets(ctx)

    assert db.calls == []


def test_orphaned_tickets_skips_a_ticket_someone_still_holds():
    jira = Jira()
    db = ScriptedDB().all_when("interval '10 minutes'", [{"key": "T-1"}])
    ctx, _events = make_ctx(db, jira=jira, leases=Leases(holders={"T-1": "run:1"}))

    orphaned_tickets(ctx)

    assert jira.status_calls == []
    assert jira.transitions == []


def test_orphaned_tickets_requeues_an_in_progress_ticket_with_no_run(monkeypatch):
    moves = []
    monkeypatch.setattr(
        "aiflow.v4.control.merge_flow.record_move",
        lambda ctx, key, old, new, cause, detail: moves.append((key, old, new, cause, detail)),
    )
    jira = Jira(status="In Progress")
    db = ScriptedDB().all_when("interval '10 minutes'", [{"key": "T-1"}])
    ctx, events = make_ctx(db, jira=jira)

    orphaned_tickets(ctx)

    assert jira.transitions == [("T-1", "To Do")]
    assert jira.comments[0][1].startswith("AI Flow · watchdog · requeued\n")
    assert ("run", "UPDATE tickets SET status='To Do' WHERE key=%s", ("T-1",)) in db.calls
    audit = next(call for call in db.calls if call[0] == "run" and "write_audit" in call[1])
    assert "To Do (orphan)" in audit[1]
    assert audit[2] == ("T-1",)
    assert moves[0][0] == "T-1" and moves[0][1:3] == ("In Progress", "To Do")
    assert "watchdog: In Progress without a run for 10 minutes" in moves[0][4]
    assert ("ticket.requeued", {"ticket": "T-1", "reason": "In Progress without a run for 10 minutes"}) in events.emitted
    assert any(item[0] == "jira.transition" and item[1]["outcome"] == "To Do" for item in events.emitted)


def test_orphaned_tickets_logs_a_jira_failure_and_does_not_emit(caplog):
    jira = Jira(fail=True)
    db = ScriptedDB().all_when("interval '10 minutes'", [{"key": "T-1"}])
    ctx, events = make_ctx(db, jira=jira)

    with caplog.at_level(logging.WARNING, logger="aiflow4.loops"):
        orphaned_tickets(ctx)

    assert "could not requeue orphan T-1: jira down" in caplog.text
    assert events.emitted == []


def test_stuck_tickets_opens_an_incident_after_three_failures():
    db = ScriptedDB().one_when(
        "account_id, summary FROM runs", {"account_id": "claude-1", "summary": "tests failed"},
    )
    db._all.append(("fails FROM streak", None))  # placeholder replaced below

    def all(sql, params=None):
        flat = " ".join(sql.split())
        db.calls.append(("all", flat, params))
        if "fails FROM streak" in flat and "make_interval" not in flat:
            if params["step"] == "dev":
                return [{"ticket_key": "T-9", "fails": 3}]
            return []
        return []

    db.all = all
    ctx, _events = make_ctx(db)

    stuck_tickets(ctx)

    opened = incident_inserts(db)[0][2]
    assert opened[0] == "ticket_stuck"
    assert opened[1] == "T-9"
    assert opened[3] == "T-9 failed 3 runs in a row"
    assert "claude-1" in opened[4]


def test_stuck_tickets_does_not_close_a_tool_wait_incident():
    db = ScriptedDB().all_when("kind='ticket_stuck'", [{"subject": "tool_wait:T-1:composer"}, {"subject": "T-2"}])
    ctx, _events = make_ctx(db)

    stuck_tickets(ctx)

    closed = [call[2][1] for call in incident_closes(db)]
    assert closed == ["T-2"]


def test_retry_and_deadline_passes_keep_going_when_the_hold_pass_fails(monkeypatch, caplog):
    monkeypatch.setattr("aiflow.v4.control.retry_hold.apply", lambda ctx: (_ for _ in ()).throw(RuntimeError("hold broke")))
    seen = []
    monkeypatch.setattr("aiflow.v4.control.escalation_deadline.apply_due", lambda ctx: seen.append("deadline"))
    ctx, _events = make_ctx(ScriptedDB(), jira=Jira())

    with caplog.at_level(logging.WARNING, logger="aiflow4.loops"):
        retry_and_deadline_passes(ctx)

    assert "retry hold pass failed: hold broke" in caplog.text
    assert seen == ["deadline"]


def test_lost_lease_requeues_when_jira_still_says_in_progress(monkeypatch):
    moves = []
    monkeypatch.setattr(run_inbox, "watchdog_lost", lambda ctx, run: (True, []))
    monkeypatch.setattr(
        "aiflow.v4.control.merge_flow.record_move",
        lambda *args: moves.append(args),
    )
    started = ago(minutes=10)
    db = ScriptedDB().all_when("status='running'", [{
        "id": 7, "fence": 3, "ticket_key": "T-1", "account_id": "a1", "node": "n1",
        "project": "P", "started_at": started,
    }])
    jira = Jira(status="In Progress")
    ctx, events = make_ctx(db, jira=jira, leases=Leases())

    lost_leases_and_run_clocks(ctx)

    assert any(call[1].startswith("UPDATE accounts SET status='idle'") for call in db.calls)
    assert ("run.lost",) == (events.emitted[0][0],)
    assert events.emitted[0][1]["reason"] == "lease expired"
    assert jira.transitions == [("T-1", "To Do")]
    audit = next(call for call in db.calls if "write_audit" in call[1])
    assert "To Do (lease lost)" in audit[1]
    assert audit[2] == ("T-1", "7", 3)
    assert any(item[0] == "jira.transition" and item[1]["reason"] == "lease lost" for item in events.emitted)


def test_run_clock_opens_the_hard_limit_and_not_the_slow_warning():
    started = ago(minutes=50)
    db = ScriptedDB().all_when("status='running'", [{
        "id": 7, "fence": 3, "ticket_key": "T-1", "account_id": "a1", "node": "n1",
        "project": "P", "started_at": started,
    }])
    ctx, _events = make_ctx(db, leases=Leases(holders={"T-1": "7:3"}))

    lost_leases_and_run_clocks(ctx)

    opened = incident_inserts(db)
    assert [call[2][0] for call in opened] == ["run_hard_limit"]
    assert opened[0][2][1] == "T-1"
    assert opened[0][2][3] == "T-1 passed 45 minutes"


def test_a_lost_run_someone_else_holds_is_not_sent_back_to_to_do(monkeypatch):
    monkeypatch.setattr(run_inbox, "watchdog_lost", lambda ctx, run: (True, []))
    db = ScriptedDB().all_when("status='running'", [{
        "id": 7, "fence": 3, "ticket_key": "T-1", "account_id": "a1", "node": "n1",
        "project": "P", "started_at": ago(minutes=1),
    }])
    jira = Jira()
    ctx, events = make_ctx(db, jira=jira, leases=Leases(holders={"T-1": "other:1"}))

    lost_leases_and_run_clocks(ctx)

    assert jira.transitions == []
    assert events.emitted[0][0] == "run.lost"


def test_account_cooldowns_release_a_finished_cooldown_and_warn_about_the_rest():
    db = ScriptedDB().all_when(
        "status='cooldown'",
        [{"id": "a1", "status": "cooldown", "cooldown_until": None}],
    ).all_when(
        "signed_out",
        [{"id": "a2", "provider": "claude", "status": "error", "cooldown_until": None, "last_error": "auth failed"}],
    ).all_when(
        "idle','working'",
        [{"id": "a3"}],
    )
    ctx, _events = make_ctx(db, leases=Leases())

    account_cooldowns(ctx)

    assert ("run", "UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s", ("a1",)) in db.calls
    assert ("account_quota", "a1") in [call[2][:2] for call in incident_closes(db)]
    opened = incident_inserts(db)[0][2]
    assert opened[:3] == ("account_quota", "a2", "warning")
    assert "fails its check" in opened[3]
    assert "auth failed" in opened[4]
    assert ("account_quota", "a3") in [call[2][:2] for call in incident_closes(db)]


def test_role_coverage_warns_when_only_one_provider_is_ready(monkeypatch):
    monkeypatch.setattr(role_health, "evaluate", lambda ctx: None)
    repo = Repo()
    repo.accounts = [SimpleNamespace(id="a1", roles=["dev"], machines=["n1"], paused=False, provider="claude")]
    db = ScriptedDB().all_when(
        "status = 'online'", [{"name": "n1"}],
    ).all_when(
        "probing'", [{"id": "a1"}],
    )
    ctx, _events = make_ctx(db, repo=repo)

    role_coverage(ctx)

    opened = [call[2] for call in incident_inserts(db) if call[2][0] == "role_coverage"]
    assert opened[0][1] == "dev"
    assert opened[0][2] == "warning"
    assert opened[0][3] == "role dev has 1 ready provider"


def test_quota_windows_opens_a_high_window_and_closes_a_recovered_one():
    db = ScriptedDB().all_when("quota ? 'windows'", [
        {"id": "high", "provider": "claude", "quota": {"windows": {
            "seven_day": {"utilization": 0.95, "resets_at": "2026-11-07T00:00:00+00:00"}}}},
        {"id": "low", "provider": "claude", "quota": {"windows": {
            "five_hour": {"utilization": 0.5, "resets_at": "2026-11-06T17:00:00+00:00"}}}},
    ])
    ctx, _events = make_ctx(db)

    quota_windows(ctx)

    opened = incident_inserts(db)[0][2]
    assert opened[0] == "quota_high"
    assert opened[1] == "high:seven_day"
    assert opened[2] == "warning"
    assert "95%" in opened[3]
    assert "Reset for free" in opened[4]
    assert ("quota_high", "low:five_hour") in [call[2][:2] for call in incident_closes(db)]


def test_quota_forecasts_stores_an_assessment_for_each_account():
    db = ScriptedDB().all_when(
        "quota_observed_at",
        [{"id": "a1", "provider": "claude", "quota": {}, "quota_source": "", "quota_observed_at": None}],
    )
    ctx, _events = make_ctx(db)

    quota_forecasts(ctx)

    saved = next(call for call in db.calls if call[0] == "run" and "quota_at_risk" in call[1])
    assert saved[2][0] is False
    assert saved[2][2] == "a1"
    assert ("quota_prediction", "a1") in [call[2][:2] for call in incident_closes(db)]
