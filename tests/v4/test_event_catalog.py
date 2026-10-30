"""The event catalog, the envelope and the listener registry (JAIF-460, JAIF-462)."""
from dataclasses import dataclass

import pytest

from aiflow.v4.control.platform.events import (CATALOG, DomainEvent, Envelope, Registry, event, from_payload,
                                                 to_payload)
from aiflow.v4.control.platform.events import catalog


@pytest.fixture(autouse=True)
def _clean_catalog():
    before = dict(CATALOG)
    yield
    CATALOG.clear()
    CATALOG.update(before)


def _make(type_="demo.thing_happened", **extra):
    @event
    @dataclass(frozen=True)
    class ThingHappened(DomainEvent):
        type = type_
        owner = "demo"
        aggregate = "thing"
        aggregate_field = "thing_id"
        thing_id: str
        count: int = 0
    return ThingHappened


def test_an_event_is_registered_and_wrapped_in_an_envelope():
    cls = _make()
    item = cls(thing_id="T-1", count=2)

    envelope = Envelope.wrap(item, causation_id="c0")

    assert CATALOG["demo.thing_happened"] is cls
    assert (envelope.type, envelope.version, envelope.aggregate, envelope.aggregate_id) == (
        "demo.thing_happened", 1, "thing", "T-1")
    assert envelope.payload == {"thing_id": "T-1", "count": 2}
    assert envelope.correlation_id == envelope.id and envelope.causation_id == "c0"
    assert envelope.occurred_at.tzinfo is not None


def test_the_payload_round_trips_and_ignores_keys_of_a_newer_version():
    cls = _make()
    item = cls(thing_id="T-1", count=3)

    assert from_payload("demo.thing_happened", to_payload(item)) == item
    assert from_payload("demo.thing_happened", {"thing_id": "T-2", "later_field": True}) == cls(thing_id="T-2")


def test_the_admission_rule_refuses_secrets_duplicates_and_incomplete_events():
    cls = _make()
    with pytest.raises(ValueError, match="already in the catalog"):
        _make()
    with pytest.raises(ValueError, match="secret-looking"):
        @event
        @dataclass(frozen=True)
        class Leaky(DomainEvent):
            type = "demo.leaky"
            owner = "demo"
            aggregate = "thing"
            aggregate_field = "thing_id"
            thing_id: str
            access_token: str = ""
    with pytest.raises(ValueError, match="must set owner"):
        @event
        @dataclass(frozen=True)
        class NoOwner(DomainEvent):
            type = "demo.no_owner"
            aggregate = "thing"
            aggregate_field = "thing_id"
            thing_id: str
    with pytest.raises(TypeError, match="frozen dataclass"):
        @event
        @dataclass
        class NotFrozen(DomainEvent):
            type = "demo.not_frozen"
            owner = "demo"
            aggregate = "thing"
            aggregate_field = "thing_id"
            thing_id: str
    assert catalog.CATALOG["demo.thing_happened"] is cls


def test_listeners_register_by_event_type_with_a_delivery_mode_and_a_unique_name():
    cls = _make()
    registry = Registry()

    @registry.on(cls)
    def audit(envelope, item): ...

    @registry.on(cls, delivery="in_transaction", name="demo.keep_count")
    def keep_count(envelope, item): ...

    assert [listener.name.rsplit(".", 1)[-1] for listener in registry.listeners_for("demo.thing_happened")] == [
        "audit", "keep_count"]
    assert [listener.name for listener in registry.listeners_for("demo.thing_happened", "in_transaction")] == [
        "demo.keep_count"]
    assert registry.listeners_for("demo.other") == []
    with pytest.raises(ValueError, match="already registered"):
        registry.on(cls, name="demo.keep_count")(lambda e, i: None)
    with pytest.raises(ValueError, match="delivery must be"):
        registry.on(cls, delivery="sometime")
