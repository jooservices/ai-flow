"""Characterize the selected-run reads before extracting them to a repository."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import FrozenInstanceError

import pytest

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
