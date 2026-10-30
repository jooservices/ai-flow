"""The dispatcher (JAIF-463): ordered per aggregate, retried with backoff, dead-lettered, replayable."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from aiflow.v4.control.platform.events import CATALOG, Dispatcher, DomainEvent, Registry, event, publish, replay

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def thing_event():
    before = dict(CATALOG)

    @event
    @dataclass(frozen=True)
    class ThingChanged(DomainEvent):
        type = "demo.thing_changed"
        owner = "demo"
        aggregate = "thing"
        aggregate_field = "thing_id"
        thing_id: str
        n: int = 0

    yield ThingChanged
    CATALOG.clear()
    CATALOG.update(before)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def _delivery(db, event_id, listener="demo.l"):
    return db.one("SELECT * FROM domain_event_deliveries WHERE event_id=%s AND listener=%s", (event_id, listener))


def test_deliveries_run_in_publish_order_per_aggregate(db, thing_event):
    registry, seen = Registry(), []
    registry.on(thing_event, name="demo.l")(lambda envelope, item: seen.append((item.thing_id, item.n)))
    for n in (1, 2, 3):
        publish(db, thing_event(thing_id="T-1", n=n), registry=registry)

    done = Dispatcher(db, registry, clock=Clock()).run_once()

    assert done == 3 and seen == [("T-1", 1), ("T-1", 2), ("T-1", 3)]
    assert db.one("SELECT count(*) AS n FROM domain_event_deliveries WHERE status='done'")["n"] >= 3


def test_a_failure_is_retried_with_backoff_and_a_later_event_waits_for_it(db, thing_event):
    registry, seen, clock = Registry(), [], Clock()
    state = {"fail": True}

    @registry.on(thing_event, name="demo.l")
    def listener(envelope, item):
        if state["fail"] and item.n == 1:
            raise RuntimeError("boom")
        seen.append(item.n)

    first = publish(db, thing_event(thing_id="T-2", n=1), registry=registry)
    publish(db, thing_event(thing_id="T-2", n=2), registry=registry)
    dispatcher = Dispatcher(db, registry, base_delay_s=10, clock=clock)

    assert dispatcher.run_once() == 1  # event 1 fails; event 2 is held back, not delivered out of order
    row = _delivery(db, first.id)
    assert (row["status"], row["attempts"], "boom" in row["last_error"]) == ("pending", 1, True)
    assert seen == []
    assert dispatcher.run_once() == 0  # not due yet

    clock.now = T0 + timedelta(seconds=11)
    state["fail"] = False
    assert dispatcher.run_once() == 2
    assert seen == [1, 2]


def test_backoff_doubles_and_the_delivery_is_dead_after_max_attempts(db, thing_event):
    registry, clock, dead = Registry(), Clock(), []
    registry.on(thing_event, name="demo.l")(lambda envelope, item: (_ for _ in ()).throw(ValueError("bad")))
    published = publish(db, thing_event(thing_id="T-3"), registry=registry)
    dispatcher = Dispatcher(db, registry, max_attempts=3, base_delay_s=10, clock=clock,
                            on_dead=lambda envelope, listener, error: dead.append((envelope.id, listener, error)))

    dispatcher.run_once()
    first_due = _delivery(db, published.id)["next_attempt_at"]
    clock.now = T0 + timedelta(seconds=11)
    dispatcher.run_once()
    second_due = _delivery(db, published.id)["next_attempt_at"]
    clock.now = T0 + timedelta(seconds=40)
    dispatcher.run_once()

    assert first_due == T0 + timedelta(seconds=10) and second_due == T0 + timedelta(seconds=11 + 20)
    row = _delivery(db, published.id)
    assert (row["status"], row["attempts"]) == ("dead", 3)
    assert dead == [(published.id, "demo.l", "ValueError: bad")]


def test_a_failed_listener_leaves_none_of_its_database_writes(db, thing_event):
    registry = Registry()
    db.run("CREATE TABLE IF NOT EXISTS zz_listener_probe (v text)")

    @registry.on(thing_event, name="demo.l")
    def listener(envelope, item):
        db.run("INSERT INTO zz_listener_probe VALUES (%s)", (item.thing_id,))
        raise RuntimeError("after writing")

    publish(db, thing_event(thing_id="T-4"), registry=registry)
    Dispatcher(db, registry, clock=Clock()).run_once()

    assert db.one("SELECT count(*) AS n FROM zz_listener_probe")["n"] == 0
    db.run("DROP TABLE zz_listener_probe")


def test_a_dead_delivery_does_not_hold_back_later_events_and_replay_revives_it(db, thing_event):
    registry, seen, state = Registry(), [], {"fail": True}

    @registry.on(thing_event, name="demo.l")
    def listener(envelope, item):
        if state["fail"] and item.n == 1:
            raise RuntimeError("down")
        seen.append(item.n)

    first = publish(db, thing_event(thing_id="T-5", n=1), registry=registry)
    publish(db, thing_event(thing_id="T-5", n=2), registry=registry)
    dispatcher = Dispatcher(db, registry, max_attempts=1, clock=Clock())

    dispatcher.run_once()
    assert seen == [2] and _delivery(db, first.id)["status"] == "dead"

    state["fail"] = False
    assert replay(db, event_id=first.id) == 1
    dispatcher.run_once()
    assert seen == [2, 1] and _delivery(db, first.id)["status"] == "done"
    assert replay(db) == 0


def test_a_delivery_for_a_listener_that_no_longer_exists_goes_dead(db, thing_event):
    registry = Registry()
    registry.on(thing_event, name="demo.old")(lambda envelope, item: None)
    published = publish(db, thing_event(thing_id="T-6"), registry=registry)

    Dispatcher(db, Registry(), max_attempts=1, clock=Clock()).run_once()

    row = _delivery(db, published.id, "demo.old")
    assert row["status"] == "dead" and "not registered" in row["last_error"]
