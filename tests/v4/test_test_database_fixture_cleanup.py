"""Failure-path tests for ownership-aware test database fixture cleanup."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.v4 import conftest


class FakeLease:
    def __init__(self, error=None):
        self.error = error
        self.closed = 0

    def close(self):
        self.closed += 1
        if self.error:
            raise self.error


class FakeDatabase:
    def __init__(self, *, close_error=None, migrate_error=None):
        self.close_error = close_error
        self.migrate_error = migrate_error
        self.closed = 0

    def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error

    def migrate(self):
        if self.migrate_error:
            raise self.migrate_error


def install_clone(monkeypatch, database=None, *, create_error=None, construct_error=None, submit_error=None):
    name, submitted = "aiflow4_t_clone", []

    def create(*args):
        if create_error:
            raise create_error
        return name

    def construct(*args):
        if construct_error:
            raise construct_error
        return database

    def submit(value):
        submitted.append(value)
        if submit_error:
            raise submit_error

    monkeypatch.setattr(conftest.testdb, "create_from_template", create)
    monkeypatch.setattr(conftest.testdb, "url_for", lambda *args: "db-url")
    monkeypatch.setattr(conftest, "DB", construct)
    return name, submitted, SimpleNamespace(submit=submit)


def install_template(monkeypatch, database=None, *, lease=None, create_error=None, drop_error=None):
    name, dropped = "aiflow4_tpl_template", []
    lease = lease or FakeLease()

    def create(*args, **kwargs):
        if create_error:
            raise create_error
        return name

    def drop(*args):
        if drop_error:
            raise drop_error
        dropped.append(args[-1])

    monkeypatch.setattr(conftest.testdb, "new_template_name", lambda: name)
    monkeypatch.setattr(conftest.testdb, "protect_template", lambda *args: lease)
    monkeypatch.setattr(conftest.testdb, "create_from_template", create)
    monkeypatch.setattr(conftest.testdb, "url_for", lambda *args: "db-url")
    monkeypatch.setattr(conftest.testdb, "drop", drop)
    monkeypatch.setattr(conftest, "DB", lambda *args, **kwargs: database or FakeDatabase())
    return name, lease, dropped


def finish(generator):
    with pytest.raises(StopIteration):
        next(generator)


def test_clone_normal_teardown_and_test_failure_close_and_submit_exactly_once(monkeypatch):
    database = FakeDatabase()
    name, submitted, dropper = install_clone(monkeypatch, database)

    generator = conftest.db.__wrapped__("test-url", "template", dropper)
    assert next(generator) is database
    finish(generator)
    assert (database.closed, submitted) == (1, [name])

    database.closed = 0
    submitted.clear()
    generator = conftest.db.__wrapped__("test-url", "template", dropper)
    assert next(generator) is database
    failure = ValueError("test body failed")
    with pytest.raises(ValueError, match="test body failed") as raised:
        generator.throw(failure)
    assert raised.value is failure
    assert (database.closed, submitted) == (1, [name])


def test_clone_creation_failure_does_not_submit_or_construct(monkeypatch):
    failure = RuntimeError("clone create failed")
    _, submitted, dropper = install_clone(monkeypatch, create_error=failure)
    generator = conftest.db.__wrapped__("test-url", "template", dropper)
    with pytest.raises(RuntimeError, match="clone create failed") as raised:
        next(generator)
    assert raised.value is failure
    assert submitted == []


def test_clone_construction_failure_submits_owned_name(monkeypatch):
    failure = RuntimeError("DB construction failed")
    name, submitted, dropper = install_clone(monkeypatch, construct_error=failure)
    generator = conftest.db.__wrapped__("test-url", "template", dropper)
    with pytest.raises(RuntimeError, match="DB construction failed") as raised:
        next(generator)
    assert (raised.value, submitted) == (failure, [name])


@pytest.mark.parametrize("body_error", [None, ValueError("test body failed")])
def test_clone_cleanup_failures_still_attempt_submission(monkeypatch, body_error):
    close_error = RuntimeError("DB close failed")
    submit_error = RuntimeError("clone submission failed")
    database = FakeDatabase(close_error=close_error)
    name, submitted, dropper = install_clone(monkeypatch, database, submit_error=submit_error)
    generator = conftest.db.__wrapped__("test-url", "template", dropper)
    next(generator)
    with pytest.raises(BaseExceptionGroup) as raised:
        generator.throw(body_error) if body_error else finish(generator)
    assert {close_error, submit_error} | ({body_error} if body_error else set()) <= set(raised.value.exceptions)
    assert (database.closed, submitted) == (1, [name])


@pytest.mark.parametrize("failure", [RuntimeError("duplicate name"), PermissionError("create denied")])
def test_template_creation_failure_never_drops_name_and_releases_lease(monkeypatch, failure):
    _, lease, dropped = install_template(monkeypatch, create_error=failure)
    generator = conftest.migrated_template.__wrapped__("test-url")
    with pytest.raises(type(failure)) as raised:
        next(generator)
    assert (raised.value, dropped, lease.closed) == (failure, [], 1)


@pytest.mark.parametrize("failure_stage", ["construct", "migrate", "close"])
def test_template_setup_failure_drops_owned_name_and_releases_lease(monkeypatch, failure_stage):
    failure = RuntimeError(f"{failure_stage} failed")
    lease = FakeLease()
    close_error = RuntimeError("template close failed") if failure_stage == "migrate" else (
        failure if failure_stage == "close" else None)
    database = FakeDatabase(migrate_error=failure if failure_stage == "migrate" else None,
                            close_error=close_error)
    name, lease, dropped = install_template(monkeypatch, database, lease=lease)
    if failure_stage == "construct":
        monkeypatch.setattr(conftest, "DB", lambda *args, **kwargs: (_ for _ in ()).throw(failure))

    generator = conftest.migrated_template.__wrapped__("test-url")
    if failure_stage == "migrate":
        with pytest.raises(BaseExceptionGroup) as raised:
            next(generator)
        assert {failure, close_error} <= set(raised.value.exceptions)
    else:
        with pytest.raises(RuntimeError, match=f"{failure_stage} failed") as raised:
            next(generator)
        assert raised.value is failure
    assert (dropped, lease.closed, database.closed) == (
        [name], 1, 1 if failure_stage in {"migrate", "close"} else 0)


@pytest.mark.parametrize("body_failure", [None, RuntimeError("test body failed")])
def test_template_teardown_drops_once_and_releases_lease(monkeypatch, body_failure):
    lease = FakeLease()
    database = FakeDatabase()
    name, lease, dropped = install_template(monkeypatch, database, lease=lease)

    generator = conftest.migrated_template.__wrapped__("test-url")
    assert next(generator) == name
    if body_failure:
        with pytest.raises(RuntimeError, match="test body failed") as raised:
            generator.throw(body_failure)
        assert raised.value is body_failure
    else:
        finish(generator)
    assert (database.closed, dropped, lease.closed) == (1, [name], 1)


def test_template_drop_failure_still_closes_lease_and_preserves_body_error(monkeypatch):
    body_error = ValueError("test body failed")
    drop_error = RuntimeError("template drop failed")
    lease = FakeLease()
    database = FakeDatabase()
    _, lease, _ = install_template(monkeypatch, database, lease=lease, drop_error=drop_error)

    generator = conftest.migrated_template.__wrapped__("test-url")
    next(generator)
    with pytest.raises(BaseExceptionGroup) as raised:
        generator.throw(body_error)
    assert {body_error, drop_error} <= set(raised.value.exceptions)
    assert (database.closed, lease.closed) == (1, 1)


def test_template_lease_close_failure_is_reported_after_drop(monkeypatch):
    lease_error = RuntimeError("lease close failed")
    lease = FakeLease(error=lease_error)
    database = FakeDatabase()
    name, lease, dropped = install_template(monkeypatch, database, lease=lease)

    generator = conftest.migrated_template.__wrapped__("test-url")
    next(generator)
    with pytest.raises(RuntimeError, match="lease close failed") as raised:
        finish(generator)
    assert raised.value is lease_error
    assert (database.closed, dropped, lease.closed) == (1, [name], 1)
