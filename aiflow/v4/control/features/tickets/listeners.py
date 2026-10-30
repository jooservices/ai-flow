"""Reactions of the tickets feature."""
from __future__ import annotations

import logging

from ...platform.events import IN_TRANSACTION, Registry
from .events import TicketMoved
from .repository import TicketRepository

log = logging.getLogger("aiflow4.api")  # the logger the inline reactions used before they became listeners


def register_listeners(ctx, registry: Registry) -> None:
    """Register the listeners that need the application context (they close over it)."""

    @registry.on(TicketMoved, delivery=IN_TRANSACTION, name="tickets.telemetry_ticket_moved")
    def telemetry_ticket_moved(envelope, item: TicketMoved) -> None:
        """Keep the `ticket.moved` telemetry row (the Command Center timeline) in the move's transaction."""
        ctx.events.emit("ticket.moved", ticket=item.ticket_key, outcome=item.to_status,
                        reason=f"{item.cause}: {item.detail}"[:300], cause=item.cause, from_status=item.from_status)

    @registry.on(TicketMoved, name="tickets.report_done_epic")
    def report_done_epic(envelope, item: TicketMoved) -> None:
        """An epic that reached Done gets its completion report (it was inline in the Jira transition and sync)."""
        if item.to_status != "Done" or item.from_status == "Done":
            return
        ticket = TicketRepository(ctx.db).get(item.ticket_key)
        if ticket is None or ticket.type != "Epic":
            return
        from ... import projects as project_lifecycle
        try:
            project_lifecycle.report_done_epic(ctx, ticket.project, ticket.key)
        except Exception as exc:  # noqa: BLE001 - a missing report must not retry the delivery forever
            log.warning("could not generate completion report for epic %s: %s", ticket.key, exc)

    @registry.on(TicketMoved, name="tickets.notify_human_review")
    def notify_human_review_started(envelope, item: TicketMoved) -> None:
        """Tell the assignee, or the owner, when a ticket enters Human review."""
        if item.to_status != "Human review" or item.from_status == "Human review":
            return
        from ...human_review_notice import notify_human_review
        notify_human_review(ctx, item.ticket_key)
