"""Domain events published by the escalation feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import DomainEvent, event


@event
@dataclass(frozen=True)
class DecisionAnswered(DomainEvent):
    """The owner (or the deadline default, or Jira) answered a question; its waiting tickets can resume."""

    type = "decision.answered"
    owner = "escalation"
    aggregate = "decision"
    aggregate_field = "decision_id"

    decision_id: int
    answer: str
    answered_by: str = ""
    waiting_tickets: tuple[str, ...] = ()
    skip_ticket: str = ""
