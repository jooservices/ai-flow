"""Domain events published by the backups feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class BackupConfigured(DomainEvent):
    type = "backup.configured"
    owner = "backups"
    aggregate = "backup"
    aggregate_field = "scope"

    actor: str
    schedule: str
    destination: str
    retention_days: int
    scope: str = "control"

    def audit(self) -> Audit:
        return Audit("backup", "configure", "", self.actor, "backup settings updated",
                     {"schedule": self.schedule, "destination": self.destination,
                      "retention_days": self.retention_days})


@event
@dataclass(frozen=True)
class BackupKeyDownloaded(DomainEvent):
    type = "backup.key_downloaded"
    owner = "backups"
    aggregate = "backup"
    aggregate_field = "scope"

    actor: str
    scope: str = "control"

    def audit(self) -> Audit:
        return Audit("backup", "download_key", "", self.actor, "one-time backup key download")


@event
@dataclass(frozen=True)
class BackupRunRequested(DomainEvent):
    type = "backup.run_requested"
    owner = "backups"
    aggregate = "backup"
    aggregate_field = "scope"

    actor: str
    scope: str = "control"

    def audit(self) -> Audit:
        return Audit("backup", "run_now", "", self.actor, "backup queued")
