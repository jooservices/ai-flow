"""Domain events published by the tickets feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import DomainEvent, event


@event
@dataclass(frozen=True)
class TicketMoved(DomainEvent):
    """A ticket changed status (who or what moved it is `cause`). Published by `merge_flow.record_move`."""

    type = "ticket.moved"
    owner = "tickets"
    aggregate = "ticket"
    aggregate_field = "ticket_key"

    ticket_key: str
    from_status: str
    to_status: str
    cause: str
    detail: str = ""
