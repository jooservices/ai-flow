"""Typed rows used by the AI review-round read model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReviewRun:
    """A completed verifier run belonging to a merged ticket."""

    ticket_key: str
    details: dict[str, Any]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ReviewRun":
        return cls(ticket_key=str(row.get("ticket_key") or ""), details=dict(row.get("details") or {}))
