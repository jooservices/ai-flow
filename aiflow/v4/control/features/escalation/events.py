"""Domain events published by the escalation feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


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
    ticket_key: str = ""
    title: str = ""

    def audit(self) -> Audit:
        return Audit("escalation", "decision-answer", self.ticket_key, self.answered_by, f"D{self.decision_id} {self.title}",
                     {"decision_id": self.decision_id})
