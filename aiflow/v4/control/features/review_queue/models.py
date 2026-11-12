"""Typed projections for the review queue's selected run-history reads."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ReviewQueueDevRun:
    ticket_key: str
    pr_url: str | None
    summary: str | None
    details: Any

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ReviewQueueDevRun":
        return cls(row["ticket_key"], row.get("pr_url"), row.get("summary"), row.get("details"))

    def to_row(self) -> dict[str, Any]:
        return {"ticket_key": self.ticket_key, "pr_url": self.pr_url,
                "summary": self.summary, "details": self.details}


@dataclass(frozen=True)
class ReviewQueueLatestRun:
    ticket_key: str
    step: str
    outcome: str | None
    summary: str | None
    details: Any
    started_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ReviewQueueLatestRun":
        return cls(row["ticket_key"], row["step"], row.get("outcome"), row.get("summary"),
                   row.get("details"), row["started_at"])

    def to_row(self) -> dict[str, Any]:
        return {"ticket_key": self.ticket_key, "step": self.step, "outcome": self.outcome,
                "summary": self.summary, "details": self.details, "started_at": self.started_at}


@dataclass(frozen=True)
class ReviewQueueVerifyRun:
    ticket_key: str
    outcome: str | None
    summary: str | None
    details: Any
    started_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ReviewQueueVerifyRun":
        return cls(row["ticket_key"], row.get("outcome"), row.get("summary"), row.get("details"),
                   row["started_at"])

    def to_row(self) -> dict[str, Any]:
        return {"ticket_key": self.ticket_key, "outcome": self.outcome, "summary": self.summary,
                "details": self.details, "started_at": self.started_at}
