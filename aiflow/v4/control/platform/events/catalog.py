"""The event catalog and the envelope every published event travels in.

A domain event is a frozen dataclass subclass of `DomainEvent`, decorated with `@event`. Its fields are the payload:
ids and small values, never secrets or whole rows. The class names the type (`ticket.moved`), the owner feature and the
payload field that holds the aggregate id. The catalog is the one place that lists every event."""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

FORBIDDEN_FIELD_PARTS = ("token", "secret", "password", "credential", "api_key")
CATALOG: dict[str, type["DomainEvent"]] = {}


@dataclass(frozen=True)
class Audit:
    """One row of the append-only write audit that an event asks for (see platform/events/audit_ledger.py)."""

    target: str
    action: str
    ticket: str
    actor: str
    result: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DomainEvent:
    """Base of every domain event; subclasses are frozen dataclasses whose fields are the payload."""

    type: ClassVar[str] = ""
    version: ClassVar[int] = 1
    owner: ClassVar[str] = ""  # the feature that publishes it
    aggregate: ClassVar[str] = ""  # kind of the aggregate, e.g. "ticket"
    aggregate_field: ClassVar[str] = ""  # payload field holding the aggregate id

    def aggregate_id(self) -> str:
        return str(getattr(self, self.aggregate_field))

    def audit(self) -> Audit | None:
        """The write-audit row of this event; an event that is audited overrides it. Routes then do not call audit()."""
        return None


@dataclass(frozen=True)
class Envelope:
    """What is stored and delivered: the event's identity and ordering keys next to its payload."""

    id: str
    type: str
    version: int
    occurred_at: datetime
    aggregate: str
    aggregate_id: str
    correlation_id: str
    causation_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def wrap(cls, item: DomainEvent, *, correlation_id: str = "", causation_id: str = "",
             now: datetime | None = None) -> "Envelope":
        """The envelope of a new event. The correlation id defaults to the event's own id (it starts a chain)."""
        event_id = uuid.uuid4().hex
        return cls(id=event_id, type=item.type, version=item.version, occurred_at=now or datetime.now(timezone.utc),
                   aggregate=item.aggregate, aggregate_id=item.aggregate_id(),
                   correlation_id=correlation_id or event_id, causation_id=causation_id, payload=to_payload(item))


def event(cls: type[DomainEvent]) -> type[DomainEvent]:
    """Register a domain event class in the catalog, refusing what the admission rule of the architecture forbids."""
    if not (isinstance(cls, type) and issubclass(cls, DomainEvent) and dataclasses.is_dataclass(cls)
            and cls.__dataclass_params__.frozen):
        raise TypeError(f"{getattr(cls, '__name__', cls)} must be a frozen dataclass subclass of DomainEvent")
    for required in ("type", "owner", "aggregate", "aggregate_field"):
        if not getattr(cls, required):
            raise ValueError(f"{cls.__name__} must set {required}")
    names = {item.name for item in dataclasses.fields(cls)}
    if cls.aggregate_field not in names:
        raise ValueError(f"{cls.__name__}.aggregate_field {cls.aggregate_field!r} is not a payload field")
    leaking = sorted(name for name in names if any(part in name.lower() for part in FORBIDDEN_FIELD_PARTS))
    if leaking:
        raise ValueError(f"{cls.__name__} carries secret-looking fields {leaking}; send an id instead")
    if cls.type in CATALOG and CATALOG[cls.type] is not cls:
        raise ValueError(f"event type {cls.type!r} is already in the catalog")
    CATALOG[cls.type] = cls
    return cls


def to_payload(item: DomainEvent) -> dict[str, Any]:
    return dataclasses.asdict(item)


def from_payload(event_type: str, payload: dict[str, Any]) -> DomainEvent:
    """Rebuild an event from a stored payload; unknown payload keys (a newer version) are ignored."""
    cls = CATALOG[event_type]
    names = {item.name for item in dataclasses.fields(cls)}
    return cls(**{key: value for key, value in payload.items() if key in names})
