"""Characterize the selected-run reads before extracting them to a repository."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import FrozenInstanceError
from threading import Barrier
from uuid import uuid4

import pytest

from aiflow.v4.control.db import DB, J
from aiflow.v4.control.features.review_queue.repository import ReviewQueueRepository
from aiflow.v4.control.review_queue_api import _load_human_review_runs


DEV_SQL = ("SELECT DISTINCT ON (ticket_key) ticket_key,pr_url,summary,details FROM runs"
           " WHERE ticket_key=ANY(%s) AND pr_url<>'' ORDER BY ticket_key,started_at DESC")
LATEST_SQL = ("SELECT DISTINCT ON (ticket_key) ticket_key,step,outcome,summary,details,started_at FROM runs"
              " WHERE ticket_key=ANY(%s) ORDER BY ticket_key,started_at DESC")
VERIFY_SQL = ("SELECT DISTINCT ON (ticket_key) ticket_key,outcome,summary,details,started_at FROM runs"
              " WHERE ticket_key=ANY(%s) AND step='verify' ORDER BY ticket_key,started_at DESC")
ASKED_SQL = ("SELECT DISTINCT ON (ticket) ticket,ts FROM events WHERE ticket=ANY(%s)"
             " AND type='review.action' AND data->>'action'='ask-ai' ORDER BY ticket,ts DESC")
TRIAGE_SQL = ("SELECT DISTINCT ON (ticket_key) ticket_key,started_at,status,outcome,ended_at FROM runs"
              " WHERE ticket_key=ANY(%s) AND step='triage' ORDER BY ticket_key,started_at DESC")


class RecordingDatabase:
    def __init__(self, rows=None, fail_on=None):
        self.rows = rows or {}
        self.fail_on = fail_on
        self.calls = []

    def all(self, sql, params=()):
        self.calls.append((sql, params))
        if self.fail_on == len(self.calls):
            raise RuntimeError(f"database failure {len(self.calls)}")
        return self.rows.get(sql, [])


def test_existing_helper_reads_three_batches_then_optional_owner_queries_with_legacy_maps():
    timestamp = datetime(2026, 2, 3, tzinfo=timezone.utc)
    rows = {
        DEV_SQL: [{"ticket_key": "A", "pr_url": "https://git/pull/7", "summary": None, "details": None}],
        LATEST_SQL: [{"ticket_key": "A", "step": "dev", "outcome": "done", "summary": "built",
                      "details": {"x": 1}, "started_at": timestamp}],
        VERIFY_SQL: [{"ticket_key": "B", "outcome": None, "summary": "", "details": None,
                      "started_at": timestamp}],
        ASKED_SQL: [{"ticket": "A", "ts": timestamp}],
        TRIAGE_SQL: [{"ticket_key": "B", "started_at": timestamp, "status": "finished",
                      "outcome": None, "ended_at": None}],
    }
    db = RecordingDatabase(rows)

    result = _load_human_review_runs(type("Context", (), {"db": db})(), ["A", "B", "A"], ["B"])

    dev, latest, verify, asked, triage = result
    assert dev == {"A": rows[DEV_SQL][0]}
    assert latest == {"A": rows[LATEST_SQL][0]}
    assert verify == {"B": rows[VERIFY_SQL][0]}
    assert asked == {"A": rows[ASKED_SQL][0]}
    assert triage == {"B": rows[TRIAGE_SQL][0]}
    assert db.calls == [(sql, (["A", "B", "A"] if sql in {DEV_SQL, LATEST_SQL, VERIFY_SQL} else ["B"],))
                        for sql in (DEV_SQL, LATEST_SQL, VERIFY_SQL, ASKED_SQL, TRIAGE_SQL)]


def test_repository_returns_frozen_rows_with_null_and_empty_values_unchanged():
    timestamp = datetime(2026, 4, 5, tzinfo=timezone.utc)
    rows = {
        DEV_SQL: [{"ticket_key": "A", "pr_url": "", "summary": None, "details": None}],
        LATEST_SQL: [{"ticket_key": "A", "step": "dev", "outcome": None, "summary": "",
                      "details": None, "started_at": timestamp}],
        VERIFY_SQL: [{"ticket_key": "A", "outcome": "passed", "summary": "", "details": {},
                      "started_at": timestamp}],
    }
    db = RecordingDatabase(rows)
    repository = ReviewQueueRepository(db)

    dev = repository.latest_pr_runs(["A"])[0]
    latest = repository.latest_runs(["A"])[0]
    verify = repository.latest_verify_runs(["A"])[0]

    assert dev.to_row() == rows[DEV_SQL][0]
    assert latest.to_row() == rows[LATEST_SQL][0]
    assert verify.to_row() == rows[VERIFY_SQL][0]
    with pytest.raises(FrozenInstanceError):
        dev.pr_url = "changed"
    assert db.calls == [(DEV_SQL, (["A"],)), (LATEST_SQL, (["A"],)), (VERIFY_SQL, (["A"],))]


def test_separate_review_queue_repositories_use_only_their_supplied_database():
    timestamp = datetime(2026, 4, 5, tzinfo=timezone.utc)
    left_db = RecordingDatabase({LATEST_SQL: [{"ticket_key": "A", "step": "dev", "outcome": "left",
                                                "summary": "", "details": {}, "started_at": timestamp}]})
    right_db = RecordingDatabase({LATEST_SQL: [{"ticket_key": "A", "step": "verify", "outcome": "right",
                                                 "summary": "", "details": {}, "started_at": timestamp}]})

    left = ReviewQueueRepository(left_db).latest_runs(["A"])
    right = ReviewQueueRepository(right_db).latest_runs(["A"])

    assert left[0].outcome == "left"
    assert right[0].outcome == "right"
    assert [sql for sql, _params in left_db.calls] == [LATEST_SQL]
    assert [sql for sql, _params in right_db.calls] == [LATEST_SQL]


@pytest.mark.parametrize("selected,owners,expected", [([], ["OWNER"], []), (["A"], [], [DEV_SQL, LATEST_SQL, VERIFY_SQL])])
def test_empty_or_ownerless_inputs_keep_the_existing_query_count(selected, owners, expected):
    db = RecordingDatabase()

    result = _load_human_review_runs(type("Context", (), {"db": db})(), selected, owners)

    assert result == ({}, {}, {}, {}, {})
    assert [sql for sql, _params in db.calls] == expected


@pytest.mark.parametrize("failed_call", [1, 2, 3])
def test_database_errors_propagate_at_the_same_run_read_without_retry(failed_call):
    db = RecordingDatabase(fail_on=failed_call)

    with pytest.raises(RuntimeError, match=f"database failure {failed_call}"):
        _load_human_review_runs(type("Context", (), {"db": db})(), ["A"], ["OWNER"])

    assert len(db.calls) == failed_call


def _insert_run(db, ticket_key, step, started_at, *, pr_url="", outcome="done", summary="", details=None):
    db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,started_at,pr_url,outcome,summary,details)"
        " VALUES (%s,%s,'TESTWP',%s,'test-account','test-node',1,%s,%s,%s,%s,%s)",
        (uuid4(), ticket_key, step, started_at, pr_url, outcome, summary, J(details or {})),
    )


def test_real_postgres_run_selection_matches_legacy_queries_for_competing_histories_and_large_duplicate_batch(db):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    t4 = datetime(2026, 1, 4, tzinfo=timezone.utc)
    t5 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    _insert_run(db, "A", "dev", t1, pr_url="https://git/pull/old", summary="old PR")
    _insert_run(db, "A", "verify", t2, outcome="rework", summary="first verify")
    _insert_run(db, "A", "dev", t3, summary="later run without PR")
    _insert_run(db, "A", "verify", t4, outcome="done", summary="latest verify")
    _insert_run(db, "A", "qa", t5, outcome="done", summary="latest overall")

    # Equal timestamps deliberately leave PostgreSQL's legacy DISTINCT ON choice unspecified.
    _insert_run(db, "B", "dev", t3, pr_url="https://git/pull/one", summary="tied dev one")
    _insert_run(db, "B", "dev", t3, pr_url="https://git/pull/two", summary="tied dev two")
    _insert_run(db, "B", "verify", t2, outcome="failed", summary="verify history")

    selected = ["A", "B", "A", *[f"ABSENT-{index}" for index in range(1200)], "B"]
    repository = ReviewQueueRepository(db)
    dev = {row.ticket_key: row.to_row() for row in repository.latest_pr_runs(selected)}
    latest = {row.ticket_key: row.to_row() for row in repository.latest_runs(selected)}
    verify = {row.ticket_key: row.to_row() for row in repository.latest_verify_runs(selected)}

    # Compare the extracted repository with the pre-extraction SQL against PostgreSQL itself.
    assert dev == {row["ticket_key"]: row for row in db.all(DEV_SQL, (selected,))}
    assert latest == {row["ticket_key"]: row for row in db.all(LATEST_SQL, (selected,))}
    assert verify == {row["ticket_key"]: row for row in db.all(VERIFY_SQL, (selected,))}
    assert dev["A"]["summary"] == "old PR"  # the later run without a PR is excluded
    assert latest["A"]["summary"] == "latest overall"
    assert verify["A"]["summary"] == "latest verify"
    assert dev["B"]["summary"] in {"tied dev one", "tied dev two"}
    assert latest["B"]["summary"] in {"tied dev one", "tied dev two"}
    assert verify["B"]["summary"] == "verify history"
    assert set(dev) == set(latest) == set(verify) == {"A", "B"}


def test_real_postgres_repositories_keep_concurrent_database_contexts_separate(db):
    left_time = datetime(2026, 5, 1, tzinfo=timezone.utc)
    right_time = datetime(2026, 5, 2, tzinfo=timezone.utc)
    _insert_run(db, "LEFT", "dev", left_time, outcome="left-context")
    _insert_run(db, "RIGHT", "verify", right_time, outcome="right-context")
    left_db, right_db = DB(db.url, min_size=1, max_size=1), DB(db.url, min_size=1, max_size=1)
    barrier = Barrier(2)

    def read(database, key):
        barrier.wait()
        return ReviewQueueRepository(database).latest_runs([key])[0]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            left_future = executor.submit(read, left_db, "LEFT")
            right_future = executor.submit(read, right_db, "RIGHT")
            left, right = left_future.result(timeout=10), right_future.result(timeout=10)
    finally:
        left_db.close()
        right_db.close()

    assert (left.ticket_key, left.outcome) == ("LEFT", "left-context")
    assert (right.ticket_key, right.outcome) == ("RIGHT", "right-context")


def test_real_postgres_repository_can_succeed_after_a_failed_read(db):
    timestamp = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_run(db, "RETRY", "verify", timestamp, outcome="done", summary="available after retry")

    class FailsOnce:
        def __init__(self, database):
            self.database = database
            self.fail = True

        def all(self, sql, params=()):
            if self.fail:
                self.fail = False
                raise RuntimeError("temporary database outage")
            return self.database.all(sql, params)

    repository = ReviewQueueRepository(FailsOnce(db))
    with pytest.raises(RuntimeError, match="temporary database outage"):
        repository.latest_runs(["RETRY"])

    rows = repository.latest_runs(["RETRY"])
    assert [(row.ticket_key, row.summary) for row in rows] == [("RETRY", "available after retry")]
