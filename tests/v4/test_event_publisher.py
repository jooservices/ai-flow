"""The outbox tables and the publisher (JAIF-461): an event commits with its change or not at all."""
from dataclasses import dataclass

import pytest

from aiflow.v4.control.platform.events import CATALOG, DomainEvent, Registry, event, publish


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
        count: int = 0

    yield ThingChanged
    CATALOG.clear()
    CATALOG.update(before)


def test_publish_stores_the_event_and_one_pending_delivery_per_outbox_listener(db, thing_event):
    registry = Registry()
    registry.on(thing_event)(lambda envelope, item: None)
    registry.on(thing_event, name="demo.second")(lambda envelope, item: None)

    envelope = publish(db, thing_event(thing_id="T-1", count=2), registry=registry)

    row = db.one("SELECT * FROM domain_events WHERE id=%s", (envelope.id,))
    assert (row["type"], row["aggregate"], row["aggregate_id"], row["payload"]) == (
        "demo.thing_changed", "thing", "T-1", {"thing_id": "T-1", "count": 2})
    assert row["correlation_id"] == envelope.id
    deliveries = db.all("SELECT listener, status, attempts FROM domain_event_deliveries WHERE event_id=%s ORDER BY listener",
                        (envelope.id,))
    assert len(deliveries) == 2 and {d["status"] for d in deliveries} == {"pending"}
    assert {d["attempts"] for d in deliveries} == {0}


def test_in_transaction_listeners_run_now_and_outbox_listeners_do_not(db, thing_event):
    registry = Registry()
    ran = []
    registry.on(thing_event, delivery="in_transaction", name="demo.now")(lambda envelope, item: ran.append(item.thing_id))
    registry.on(thing_event, name="demo.later")(lambda envelope, item: ran.append("later"))

    envelope = publish(db, thing_event(thing_id="T-2"), registry=registry)

    assert ran == ["T-2"]
    assert [d["listener"] for d in db.all("SELECT listener FROM domain_event_deliveries WHERE event_id=%s",
                                          (envelope.id,))] == ["demo.later"]


def test_the_event_rolls_back_with_the_change_that_failed(db, thing_event):
    registry = Registry()

    class Boom(Exception):
        pass

    def fail(envelope, item):
        raise Boom

    registry.on(thing_event, delivery="in_transaction", name="demo.fail")(fail)
    before = db.one("SELECT count(*) AS n FROM domain_events")["n"]

    with pytest.raises(Boom), db.transaction():
        publish(db, thing_event(thing_id="T-3"), registry=registry)

    assert db.one("SELECT count(*) AS n FROM domain_events")["n"] == before


def test_events_get_increasing_sequence_numbers_per_publish(db, thing_event):
    first = publish(db, thing_event(thing_id="T-4"), registry=Registry())
    second = publish(db, thing_event(thing_id="T-4"), registry=Registry())

    seqs = [db.one("SELECT seq FROM domain_events WHERE id=%s", (e.id,))["seq"] for e in (first, second)]
    assert seqs[0] < seqs[1]
