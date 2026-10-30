"""The audit ledger as a listener: an event that overrides `DomainEvent.audit()` gets its write-audit row here.

One in-transaction listener per such event type, so the row commits with the change and the ledger stays append-only (it
goes through the same `route_deps.audit` as before). A route no longer calls `audit()` for an event that audits itself."""
from __future__ import annotations

from typing import Any

from .catalog import CATALOG, DomainEvent, Envelope
from .registry import IN_TRANSACTION, Registry


def audited_types() -> list[str]:
    return sorted(event_type for event_type, cls in CATALOG.items() if cls.audit is not DomainEvent.audit)


def register_listeners(ctx: Any, registry: Registry) -> None:
    from ... import route_deps

    for event_type in audited_types():
        def write(envelope: Envelope, item: DomainEvent) -> None:
            entry = item.audit()
            if entry is not None:
                route_deps.audit(ctx, entry.target, entry.action, entry.ticket, "", None, entry.actor, True,
                                 entry.result, entry.payload)

        registry.on(CATALOG[event_type], delivery=IN_TRANSACTION, name=f"audit.{event_type}")(write)
