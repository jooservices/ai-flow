"""Domain events published by the work feature: a worker started a run, and a run finished."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import DomainEvent, event


@event
@dataclass(frozen=True)
class RunStarted(DomainEvent):
    """A worker claimed a ticket step: the lease, the run row and the account are set."""

    type = "run.started"
    owner = "work"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    ticket_key: str
    project: str
    step: str
    fence: int
    account: str
    provider: str
    model: str
    node: str


@event
@dataclass(frozen=True)
class RunFinished(DomainEvent):
    """A run ended (a worker result, or a reviewer action in the console)."""

    type = "run.finished"
    owner = "work"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    ticket_key: str
    project: str
    step: str
    fence: int
    account: str
    provider: str
    node: str
    outcome: str
    reason: str = ""
    model: str = ""
    duration_ms: int | None = None
    usage_in: int | None = None
    usage_out: int | None = None
    cost_usd: float | None = None
    still_owner: bool | None = None
    pr_url: str = ""
