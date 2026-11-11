"""Characterize incident-open persistence before and after its repository extraction."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import psycopg
import pytest

from aiflow.v4.control import incidents
from aiflow.v4.control.features.incidents import repository as incident_repository
from aiflow.v4.control.platform.events import wiring


def test_opening_a_closed_incident_creates_a_fresh_open_row_and_event(ctx, db):
    assert incidents.open_incident(ctx, "reopen_case", "subject", "info", "first", "initial") is True
    first = db.one("SELECT id FROM incidents WHERE kind=%s AND subject=%s", ("reopen_case", "subject"))
    db.run("UPDATE incidents SET closed_at=now() WHERE id=%s", (first["id"],))

    assert incidents.open_incident(ctx, "reopen_case", "subject", "warning", "second", "reopened") is True

    rows = db.all("SELECT id,closed_at,title,detail FROM incidents WHERE kind=%s AND subject=%s ORDER BY id",
                  ("reopen_case", "subject"))
    assert len(rows) == 2
    assert rows[0]["id"] == first["id"] and rows[0]["closed_at"] is not None
    assert rows[0]["title"] == "first" and rows[0]["detail"] == "initial"
    assert rows[1]["closed_at"] is None and rows[1]["title"] == "second" and rows[1]["detail"] == "reopened"
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='incident.opened'")["n"] == 2


def test_concurrent_open_attempts_insert_and_publish_only_once(ctx, db):
    barrier = Barrier(2)

    def open_at_once() -> bool:
        barrier.wait(timeout=5)
        return incidents.open_incident(ctx, "concurrent_case", "same-subject", "info", "title", "detail")

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: open_at_once(), range(2)))

    assert sorted(results) == [False, True]
    assert db.one("SELECT count(*) AS n FROM incidents WHERE kind=%s AND subject=%s AND closed_at IS NULL",
                  ("concurrent_case", "same-subject"))["n"] == 1
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='incident.opened'")["n"] == 1


def test_open_incident_preserves_empty_quoted_sql_like_and_long_values(ctx, db):
    kind = "quoted'kind"
    subject = "'; DROP TABLE incidents; --"
    title = ""
    detail = "prefix ' ; DELETE FROM incidents; -- " + "x" * 5000

    assert incidents.open_incident(ctx, kind, subject, "info", title, detail) is True

    row = db.one("SELECT kind,subject,title,detail FROM incidents WHERE kind=%s AND subject=%s", (kind, subject))
    assert row == {"kind": kind, "subject": subject, "title": title, "detail": detail}
    assert db.one("SELECT count(*) AS n FROM incidents")["n"] == 1


def test_invalid_null_value_propagates_database_error_without_publishing(ctx, db, monkeypatch):
    published = []
    monkeypatch.setattr(wiring, "publish_for", lambda *args, **kwargs: published.append(args))

    with pytest.raises(psycopg.errors.NotNullViolation):
        incidents.open_incident(ctx, "null_case", "subject", "info", None, "detail")

    assert published == []
    assert db.one("SELECT count(*) AS n FROM incidents")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM domain_events")["n"] == 0


def test_insert_error_propagates_without_publishing(monkeypatch):
    published = []

    class FailingDB:
        def one(self, sql, params):
            raise RuntimeError("insert failed")

    ctx = SimpleNamespace(db=FailingDB())
    monkeypatch.setattr(wiring, "publish_for", lambda *args, **kwargs: published.append(args))

    with pytest.raises(RuntimeError, match="insert failed"):
        incidents.open_incident(ctx, "error_case", "subject", "info", "title")

    assert published == []


def test_publish_failure_keeps_the_insert_outside_the_event_transaction(ctx, db, monkeypatch):
    def fail_publish(*args, **kwargs):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(wiring, "publish_for", fail_publish)

    with pytest.raises(RuntimeError, match="publish failed"):
        incidents.open_incident(ctx, "publish_error_case", "subject", "info", "title")

    assert db.one("SELECT count(*) AS n FROM incidents WHERE kind=%s", ("publish_error_case",))["n"] == 1
    assert db.one("SELECT count(*) AS n FROM domain_events")["n"] == 0


def test_repository_insert_preserves_sql_parameters_and_returns_inserted_id():
    class RecordingDB:
        query = None
        params = None

        def one(self, query, params):
            self.query, self.params = query, params
            return {"id": 41}

    db = RecordingDB()
    values = ("kind", "subject", "warning", "title", "detail")

    assert incident_repository.insert_open_incident(db, *values) == 41

    assert db.query == ("INSERT INTO incidents (kind, subject, severity, title, detail) VALUES (%s,%s,%s,%s,%s)"
                        " ON CONFLICT (kind, subject) WHERE closed_at IS NULL DO NOTHING RETURNING id")
    assert db.params == values


def test_repository_insert_returns_none_when_conflict_prevents_insertion():
    class EmptyDB:
        def one(self, query, params):
            return None

    assert incident_repository.insert_open_incident(
        EmptyDB(), "kind", "subject", "warning", "title", "detail") is None


@pytest.mark.parametrize("inserted_id,expected", [(41, True), (None, False)])
def test_open_incident_delegates_to_repository_and_publishes_only_after_insert(
        monkeypatch, inserted_id, expected):
    calls = []
    published = []
    db = object()
    ctx = SimpleNamespace(db=db)
    values = ("kind", "subject", "critical", "title", "detail")

    def insert_open_incident(*args):
        calls.append(args)
        return inserted_id

    monkeypatch.setattr(incident_repository, "insert_open_incident", insert_open_incident)
    monkeypatch.setattr(wiring, "publish_for", lambda *args, **kwargs: published.append((args, kwargs)))

    assert incidents.open_incident(ctx, *values) is expected

    assert calls == [(db, *values)]
    if inserted_id is None:
        assert published == []
    else:
        (published_ctx, event), kwargs = published[0]
        assert published_ctx is ctx
        assert (event.incident_id, event.kind, event.subject, event.severity, event.title, event.detail) == (
            inserted_id, *values)
        assert kwargs == {"deliver_now": True}
