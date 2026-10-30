"""Reactions of the tickets feature."""
from __future__ import annotations

from ...platform.events import IN_TRANSACTION, Registry
from .events import TicketMoved


def register_listeners(ctx, registry: Registry) -> None:
    """Register the listeners that need the application context (they close over it)."""

    @registry.on(TicketMoved, delivery=IN_TRANSACTION, name="tickets.telemetry_ticket_moved")
    def telemetry_ticket_moved(envelope, item: TicketMoved) -> None:
        """Keep the `ticket.moved` telemetry row (the Command Center timeline) in the move's transaction."""
        ctx.events.emit("ticket.moved", ticket=item.ticket_key, outcome=item.to_status,
                        reason=f"{item.cause}: {item.detail}"[:300], cause=item.cause, from_status=item.from_status)
