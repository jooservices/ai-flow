"""Reactions of the escalation feature."""
from __future__ import annotations

from ...platform.events import Registry
from .events import DecisionAnswered


def register_listeners(ctx, registry: Registry) -> None:
    @registry.on(DecisionAnswered, name="escalation.resume_waiting_tickets")
    def resume_waiting_tickets(envelope, item: DecisionAnswered) -> None:
        """Move the tickets that waited for this answer back to work (Jira comment, status, owner labels).

        `resume_waiting` keeps a failed ticket in `resuming` for `retry_resumptions`, so this listener never raises."""
        from ... import escalations
        from ...repositories import escalations as escalation_repository
        decision = escalation_repository.decision_by_id(ctx.db, item.decision_id)
        if decision is None:
            return
        escalations.resume_waiting(ctx, {**decision, "waiting_tickets": list(item.waiting_tickets)}, item.answer,
                                   skip_ticket=item.skip_ticket)
