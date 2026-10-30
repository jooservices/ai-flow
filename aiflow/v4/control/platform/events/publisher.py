"""Publish a domain event: write it to the outbox in the caller's transaction and run its in-transaction listeners."""
from __future__ import annotations

from typing import Any

from ...db import J
from .catalog import DomainEvent, Envelope
from .registry import DEFAULT_REGISTRY, IN_TRANSACTION, OUTBOX, Registry


def publish(db: Any, item: DomainEvent, *, correlation_id: str = "", causation_id: str = "",
            registry: Registry = DEFAULT_REGISTRY) -> Envelope:
    """Store `item` and one pending delivery per outbox listener; run in-transaction listeners before returning.

    Call it inside the transaction of the change the event describes (`with db.transaction():`): the event commits with
    the change or not at all. A failing in-transaction listener raises and rolls the whole change back; outbox listeners
    never run here, so they cannot fail the caller."""
    envelope = Envelope.wrap(item, correlation_id=correlation_id, causation_id=causation_id)
    with db.transaction():
        db.run("INSERT INTO domain_events (id, type, version, occurred_at, aggregate, aggregate_id, correlation_id,"
               " causation_id, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (envelope.id, envelope.type, envelope.version, envelope.occurred_at, envelope.aggregate,
                envelope.aggregate_id, envelope.correlation_id, envelope.causation_id, J(envelope.payload)))
        for listener in registry.listeners_for(envelope.type, OUTBOX):
            db.run("INSERT INTO domain_event_deliveries (event_id, listener) VALUES (%s,%s)",
                   (envelope.id, listener.name))
        for listener in registry.listeners_for(envelope.type, IN_TRANSACTION):
            listener.handler(envelope, item)
    return envelope
