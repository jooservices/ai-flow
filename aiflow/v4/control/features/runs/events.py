"""Domain events published by successful master writes in the runs feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class MasterDailyBudgetUpdated(DomainEvent):
    type = "master.daily_budget_updated"
    owner = "runs"
    aggregate = "master_budget"
    aggregate_field = "scope"

    actor: str
    daily_budget_minutes: int
    scope: str = "control"

    def audit(self) -> Audit:
        return Audit("master", "daily-budget-update", "", self.actor,
                     f"daily budget set to {self.daily_budget_minutes} minutes",
                     {"daily_budget_minutes": self.daily_budget_minutes})


@event
@dataclass(frozen=True)
class MasterActionCompleted(DomainEvent):
    type = "master.action_completed"
    owner = "runs"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    action: str
    result: str
    intent: str
    why: str
    account_id: str
    target_run_id: str
    signature: str

    def audit(self) -> Audit:
        return Audit("master", self.action, self.ticket, self.actor, self.result,
                     {"intent": self.intent, "why": self.why, "account_id": self.account_id,
                      "target_run_id": self.target_run_id, "signature": self.signature},
                     self.run_id, self.fence)


@event
@dataclass(frozen=True)
class MasterEscalationRequested(DomainEvent):
    type = "master.escalation_requested"
    owner = "runs"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    action: str
    reason: str
    intent_chars: int
    observation_key: str
    options: list[str]

    def audit(self) -> Audit:
        return Audit("master", self.action, self.ticket, self.actor, self.reason,
                     {"intent_chars": self.intent_chars, "observation_key": self.observation_key,
                      "options": self.options}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class MasterEscalationNotified(DomainEvent):
    type = "master.escalation_notified"
    owner = "runs"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    result: str
    reason: str
    options: list[str]
    observation_key: str
    pending_channels: list[str]

    def audit(self) -> Audit:
        return Audit("notify", "master-escalation", self.ticket, self.actor, self.result,
                     {"reason": self.reason, "options": self.options, "observation_key": self.observation_key,
                      "pending_channels": self.pending_channels}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class FlowBugCreated(DomainEvent):
    # `flow_bug.created` is already used by the telemetry log.
    type = "flow_bug.issue_created"
    owner = "runs"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    reason: str
    signature: str
    run_ids: list[str]
    tickets: list[str]
    labels: list[str]

    def audit(self) -> Audit:
        return Audit("jira", "flow_bug_create", self.ticket, self.actor, "created",
                     {"reason": self.reason, "signature": self.signature, "run_ids": self.run_ids,
                      "tickets": self.tickets, "labels": self.labels}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class FlowBugRepeated(DomainEvent):
    # `flow_bug.repeated` is already used by the telemetry log.
    type = "flow_bug.repeat_commented"
    owner = "runs"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    comment_id: str
    signature: str
    repeat_count: int
    new_run_ids: list[str]

    def audit(self) -> Audit:
        return Audit("jira", "flow_bug_repeat", self.ticket, self.actor, self.comment_id,
                     {"signature": self.signature, "repeat_count": self.repeat_count,
                      "new_run_ids": self.new_run_ids}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class FlowBugIntakeMoved(DomainEvent):
    type = "flow_bug.intake_moved"
    owner = "runs"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    signature: str

    def audit(self) -> Audit:
        return Audit("jira", "flow_bug_intake", self.ticket, self.actor, "Intake",
                     {"signature": self.signature}, self.run_id, self.fence)
