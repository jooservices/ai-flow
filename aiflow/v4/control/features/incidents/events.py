"""Domain events published by the incidents feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import DomainEvent, event


@event
@dataclass(frozen=True)
class IncidentOpened(DomainEvent):
    """A new incident was opened for (kind, subject); an incident that was already open does not publish."""

    type = "incident.opened"
    owner = "incidents"
    aggregate = "incident"
    aggregate_field = "incident_id"

    incident_id: int
    kind: str
    subject: str
    severity: str
    title: str
    detail: str = ""
