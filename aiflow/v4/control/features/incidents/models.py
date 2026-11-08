"""Typed records owned by the incident and coordinator handoff feature."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    ticket_key: str
    step: str
    node: str
    account_id: str
    provider: str
    model: str
    started_at: datetime
    ended_at: datetime | None
    heartbeat_at: datetime
    status: str
    outcome: str
    error_kind: str
    summary: str
    progress: dict[str, Any]
    details: dict[str, Any]
    tokens_in: int
    tokens_out: int
    cost_usd: Any
    node_heartbeat_at: datetime | None
    node_metrics: dict[str, Any]


@dataclass(frozen=True)
class FailureRun:
    run_id: str
    outcome: str
    started_at: datetime
    ended_at: datetime
    diagnostic: dict[str, Any]


@dataclass(frozen=True)
class TicketHandoff:
    ticket_key: str
    incident_id: int
    state: str
    title: str
    detail: str
    resolved_at: datetime | None
