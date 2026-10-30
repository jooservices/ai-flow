"""Entities of the work feature: frozen dataclasses, no imports from control."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AccountForecast:
    id: str
    quota_forecast: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeTools:
    tools: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {"tools": self.tools, "metrics": self.metrics}


@dataclass(frozen=True)
class RunNote:
    """What an earlier run of a ticket said: its summary, details and when it started."""

    summary: str
    details: dict[str, Any]
    started_at: datetime


@dataclass(frozen=True)
class BoardTicket:
    key: str
    type: str
    status: str
    summary: str
    parent_key: str | None
