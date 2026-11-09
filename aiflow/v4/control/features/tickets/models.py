"""Entities of the tickets feature: frozen dataclasses, no imports from control."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    jira_created: datetime | None = None
    jira_updated: datetime | None = None
    done_at: datetime | None = None
    parent_changed_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Ticket":
        """Build the immutable ticket model from a repository row."""
        return cls(key=row["key"], project=row["project"], status=row["status"], type=row.get("type") or "Task",
                   priority=row.get("priority") or "Medium", summary=row.get("summary") or "",
                   description=row.get("description") or "", labels=tuple(row.get("labels") or ()),
                   parent_key=row.get("parent_key"), blocked_by=tuple(row.get("blocked_by") or ()),
                   jira_created=row.get("jira_created"), jira_updated=row.get("jira_updated"),
                   done_at=row.get("done_at"), parent_changed_at=row.get("parent_changed_at"))


@dataclass(frozen=True)
class TicketMove:
    """The latest recorded status change for a ticket."""

    ticket: str
    ts: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TicketMove":
        """Build the immutable move model from a repository row."""
        return cls(ticket=row["ticket"], ts=row["ts"])


@dataclass(frozen=True)
class EpicTransitionFollowup:
    """Durable work left by one automatic epic transition."""

    id: int
    ticket: str
    project: str
    from_status: str
    to_status: str
    source: str
    reason: str
    child_count: int
    completed_at: datetime
    comment: str
    ready: bool
    moved: bool
    status_event_done: bool
    report_done: bool
    transition_event_done: bool
    move_done: bool
    audit_done: bool
    comment_done: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "EpicTransitionFollowup":
        """Build a frozen follow-up model from its repository row."""
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})
