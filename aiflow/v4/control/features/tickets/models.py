"""Entities of the tickets feature: frozen dataclasses, no imports from control."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Ticket:
    """A ticket as mirrored from Jira (the columns every use case needs; the rest stays in the row)."""

    key: str
    project: str
    status: str
    type: str = "Task"
    priority: str = "Medium"
    summary: str = ""
    description: str = ""
    labels: tuple[str, ...] = ()
    parent_key: str | None = None
    blocked_by: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Ticket":
        return cls(key=row["key"], project=row["project"], status=row["status"], type=row.get("type") or "Task",
                   priority=row.get("priority") or "Medium", summary=row.get("summary") or "",
                   description=row.get("description") or "", labels=tuple(row.get("labels") or ()),
                   parent_key=row.get("parent_key"), blocked_by=tuple(row.get("blocked_by") or ()))
