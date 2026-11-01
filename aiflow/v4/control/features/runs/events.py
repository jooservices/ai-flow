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
