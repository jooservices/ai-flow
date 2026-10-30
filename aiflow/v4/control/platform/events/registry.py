"""The listener registry: `@on(Event)` registers a reaction; the dispatcher asks which listeners an event type has."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .catalog import DomainEvent

IN_TRANSACTION = "in_transaction"  # runs inside the publisher's transaction and commits with the change
OUTBOX = "outbox"  # runs after commit, in order per aggregate, with retry; the default
DELIVERY_MODES = (IN_TRANSACTION, OUTBOX)


@dataclass(frozen=True)
class Listener:
    name: str
    event_type: str
    handler: Callable[..., None]
    delivery: str


class Registry:
    def __init__(self) -> None:
        self._by_type: dict[str, list[Listener]] = {}
        self._names: set[str] = set()

    def on(self, event_class: type[DomainEvent], *, delivery: str = OUTBOX, name: str = ""):
        """Decorator: register `handler(envelope, event)` for `event_class`. The name must be unique (it keys retries)."""
        if delivery not in DELIVERY_MODES:
            raise ValueError(f"delivery must be one of {DELIVERY_MODES}")

        def register(handler: Callable[..., None]) -> Callable[..., None]:
            listener_name = name or f"{handler.__module__}.{handler.__qualname__}"
            if listener_name in self._names:
                raise ValueError(f"listener {listener_name!r} is already registered")
            self._names.add(listener_name)
            self._by_type.setdefault(event_class.type, []).append(Listener(listener_name, event_class.type, handler, delivery))
            return handler

        return register

    def listeners_for(self, event_type: str, delivery: str | None = None) -> list[Listener]:
        found = self._by_type.get(event_type, [])
        return [item for item in found if delivery is None or item.delivery == delivery]

    def all(self) -> list[Listener]:
        return [item for items in self._by_type.values() for item in items]


DEFAULT_REGISTRY = Registry()
on = DEFAULT_REGISTRY.on
