"""The third consecutive unsuccessful run holds the ticket (JAIF-367)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from aiflow.v4.control import retry_hold
from tests.v4.conftest import add_ticket


class Jira:
    def __init__(self, fail: bool = False):
        self.labels: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.fail = fail

    def add_label(self, key, label):
        if self.fail:
            raise RuntimeError("jira down")
        self.labels.append((key, label))

    def comment(self, key, text):
        self.comments.append((key, text))
        return "1"


class Events:
    def __init__(self):
        self.seen: list[tuple[str, dict]] = []

    def emit(self, name, **kw):
        self.seen.append((name, kw))


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db=db, jira=Jira(), events=Events())


def run(db, key, outcome, minutes_ago, step="dev"):
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence, status, outcome, error_kind,"
           " started_at, ended_at, summary) VALUES (%s,%s,'TESTWP',%s,'codex-01','node1',1,'finished',%s,%s,"
           " now() - make_interval(mins => %s), now() - make_interval(mins => %s) + interval '2 minutes', 'x')",
           (str(uuid.uuid4()), key, step, outcome, "timeout" if outcome == "timeout" else "", minutes_ago, minutes_ago))


def test_two_failures_do_not_hold_but_the_third_does(db, ctx):
    add_ticket(db, "TESTWP-1")
    run(db, "TESTWP-1", "timeout", 30)
    run(db, "TESTWP-1", "failed", 20)
    assert retry_hold.apply(ctx) == []
    run(db, "TESTWP-1", "no_changes", 10)
    assert retry_hold.apply(ctx) == ["TESTWP-1"]
    assert ctx.jira.labels == [("TESTWP-1", "aiflow-hold")]
    assert "aiflow-hold" in db.one("SELECT labels FROM tickets WHERE key='TESTWP-1'")["labels"]
    assert len(ctx.jira.comments) == 1 and ctx.jira.comments[0][1].count("- run ") == 3
    assert any(name == "ticket.held" for name, _ in ctx.events.seen)


def test_streak_covers_the_whole_history_not_only_the_last_day(db, ctx):
    add_ticket(db, "TESTWP-2")
    for days in (5, 4, 3):
        run(db, "TESTWP-2", "timeout", days * 24 * 60)
    assert retry_hold.apply(ctx) == ["TESTWP-2"]


def test_a_run_that_produced_work_ends_the_streak(db, ctx):
    add_ticket(db, "TESTWP-3")
    run(db, "TESTWP-3", "failed", 50)
    run(db, "TESTWP-3", "failed", 40)
    run(db, "TESTWP-3", "done", 30)
    run(db, "TESTWP-3", "failed", 20)
    assert retry_hold.apply(ctx) == []


def test_hold_is_not_repeated_and_blocks_the_comment_duplicate(db, ctx):
    add_ticket(db, "TESTWP-4")
    for minutes in (30, 20, 10):
        run(db, "TESTWP-4", "failed", minutes)
    assert retry_hold.apply(ctx) == ["TESTWP-4"]
    assert retry_hold.apply(ctx) == []
    assert len(ctx.jira.comments) == 1


def test_removing_the_label_releases_and_restarts_the_streak(db, ctx):
    add_ticket(db, "TESTWP-5")
    for minutes in (30, 20, 10):
        run(db, "TESTWP-5", "failed", minutes)
    retry_hold.apply(ctx)
    db.run("UPDATE tickets SET labels = '{aiflow-ready}' WHERE key='TESTWP-5'")  # the coordinator removes the label
    retry_hold.apply(ctx)
    assert db.one("SELECT released_at FROM retry_holds WHERE ticket_key='TESTWP-5'")["released_at"] is not None
    assert retry_hold.streaks(db).get("TESTWP-5", (0, ""))[0] == 0
    run(db, "TESTWP-5", "failed", -1)  # one new failure after the release does not hold again
    assert retry_hold.apply(ctx) == []


def test_watchdog_requeues_count_towards_the_streak(db, ctx):
    add_ticket(db, "TESTWP-6", status="To Do")
    for minutes in (9, 6, 3):
        db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, ts) VALUES"
               " ('jira','transition','TESTWP-6','control',true,'To Do (orphan)', now() - make_interval(mins => %s))",
               (minutes,))
    assert retry_hold.apply(ctx) == ["TESTWP-6"]


def test_a_jira_failure_leaves_no_hold_row_so_the_next_pass_retries(db):
    add_ticket(db, "TESTWP-7")
    for minutes in (30, 20, 10):
        run(db, "TESTWP-7", "failed", minutes)
    broken = SimpleNamespace(db=db, jira=Jira(fail=True), events=Events())
    assert retry_hold.apply(broken) == []
    assert db.one("SELECT count(*) AS n FROM retry_holds")["n"] == 0
    assert retry_hold.apply(SimpleNamespace(db=db, jira=Jira(), events=Events())) == ["TESTWP-7"]


def test_done_tickets_and_running_runs_are_ignored(db, ctx):
    add_ticket(db, "TESTWP-8", status="Done")
    for minutes in (30, 20, 10):
        run(db, "TESTWP-8", "failed", minutes)
    add_ticket(db, "TESTWP-9")
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence, status) VALUES"
           " (%s,'TESTWP-9','TESTWP','dev','codex-01','node1',1,'running')", (str(uuid.uuid4()),))
    assert retry_hold.apply(ctx) == []


def test_a_failed_comment_still_leaves_the_hold_recorded(db):
    add_ticket(db, "TESTWP-10")
    for minutes in (30, 20, 10):
        run(db, "TESTWP-10", "failed", minutes)

    class NoComment(Jira):
        def comment(self, key, text):
            raise RuntimeError("comment failed")

    ctx = SimpleNamespace(db=db, jira=NoComment(), events=Events())
    assert retry_hold.apply(ctx) == ["TESTWP-10"]
    assert db.one("SELECT count(*) AS n FROM retry_holds WHERE ticket_key='TESTWP-10' AND released_at IS NULL")["n"] == 1
    db.run("UPDATE tickets SET labels = '{aiflow-ready}' WHERE key='TESTWP-10'")  # releasing resets the streak
    retry_hold.apply(ctx)
    assert retry_hold.streaks(db).get("TESTWP-10", (0, ""))[0] == 0


def test_a_label_without_a_row_gets_its_row_and_no_second_jira_write(db, ctx):
    add_ticket(db, "TESTWP-11", labels=("aiflow-ready", "aiflow-hold"))
    for minutes in (30, 20, 10):
        run(db, "TESTWP-11", "failed", minutes)
    assert retry_hold.apply(ctx) == []
    assert ctx.jira.labels == [] and ctx.jira.comments == []
    assert db.one("SELECT count(*) AS n FROM retry_holds WHERE ticket_key='TESTWP-11' AND released_at IS NULL")["n"] == 1
