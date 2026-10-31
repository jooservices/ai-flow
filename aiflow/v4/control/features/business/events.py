"""Domain events published by the business feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class ChangeRequestFiled(DomainEvent):
    type = "business.change_request_filed"
    owner = "business"
    aggregate = "business_project"
    aggregate_field = "project"

    project: str
    actor: str
    ticket: str
    status: str
    epic: str
    chars: int
    labels: list[str]

    def audit(self) -> Audit:
        return Audit("jira", "create", self.ticket, self.actor, self.status,
                     {"project": self.project, "type": "Story", "labels": self.labels, "epic": self.epic,
                      "chars": self.chars})


@event
@dataclass(frozen=True)
class StakeholderAccepted(DomainEvent):
    type = "business.stakeholder_accepted"
    owner = "business"
    aggregate = "business_project"
    aggregate_field = "project"

    project: str
    actor: str
    ticket: str
    comment_id: str
    change: str
    environment: str

    def audit(self) -> Audit:
        return Audit("jira", "stakeholder_acceptance", self.ticket, self.actor, self.comment_id,
                     {"project": self.project, "change": self.change, "environment": self.environment})


@event
@dataclass(frozen=True)
class StakeholderBugFiled(DomainEvent):
    type = "business.stakeholder_bug_filed"
    owner = "business"
    aggregate = "business_project"
    aggregate_field = "project"

    project: str
    actor: str
    ticket: str
    status: str
    change: str
    environment: str
    screenshot: bool

    def audit(self) -> Audit:
        return Audit("jira", "stakeholder_bug", self.ticket, self.actor, self.status,
                     {"project": self.project, "change": self.change, "environment": self.environment,
                      "screenshot": self.screenshot})
