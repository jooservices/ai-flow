"""Domain events published by the console routes when they write audit rows."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class ViewAsStarted(DomainEvent):
    type = "console.view_as_started"
    owner = "console"
    aggregate = "console_user"
    aggregate_field = "actor"

    actor: str
    role: str

    def audit(self) -> Audit:
        return Audit("console", "view_as", "", self.actor, "Administrator viewing as role", {"role": self.role})


@event
@dataclass(frozen=True)
class ViewAsStopped(DomainEvent):
    type = "console.view_as_stopped"
    owner = "console"
    aggregate = "console_user"
    aggregate_field = "actor"

    actor: str
    previous_role: str

    def audit(self) -> Audit:
        return Audit("console", "view_as_stop", "", self.actor, "Administrator returned to own role",
                     {"previous_role": self.previous_role})


@event
@dataclass(frozen=True)
class NotificationChannelSaved(DomainEvent):
    type = "notifications.channel_saved"
    owner = "console"
    aggregate = "notification"
    aggregate_field = "actor"

    actor: str
    channel_id: int
    kind: str
    enabled: bool

    def audit(self) -> Audit:
        return Audit("notifications", "channel_save", "", self.actor, "notification channel saved",
                     {"channel_id": self.channel_id, "kind": self.kind, "enabled": self.enabled})


@event
@dataclass(frozen=True)
class NotificationChannelDeleted(DomainEvent):
    type = "notifications.channel_deleted"
    owner = "console"
    aggregate = "notification"
    aggregate_field = "actor"

    actor: str
    channel_id: int

    def audit(self) -> Audit:
        return Audit("notifications", "channel_delete", "", self.actor, "notification channel deleted",
                     {"channel_id": self.channel_id})


@event
@dataclass(frozen=True)
class NotificationPreferencesSaved(DomainEvent):
    type = "notifications.preferences_saved"
    owner = "console"
    aggregate = "notification"
    aggregate_field = "actor"

    actor: str
    route_count: int
    quiet_hours_enabled: bool

    def audit(self) -> Audit:
        return Audit("notifications", "preferences_save", "", self.actor,
                     "notification routing preferences saved",
                     {"route_count": self.route_count, "quiet_hours_enabled": self.quiet_hours_enabled})


@event
@dataclass(frozen=True)
class LicenceActivated(DomainEvent):
    type = "licensing.license_activated"
    owner = "console"
    aggregate = "licence"
    aggregate_field = "actor"

    actor: str
    source: str
    plan: str

    def audit(self) -> Audit:
        return Audit("licensing", "license_activate", "", self.actor, "activated " + self.source,
                     {"source": self.source, "plan": self.plan})


@event
@dataclass(frozen=True)
class LicenceLimitsUpdated(DomainEvent):
    type = "licensing.limits_updated"
    owner = "console"
    aggregate = "licence"
    aggregate_field = "actor"

    actor: str
    plan: str
    limits: dict[str, int]

    def audit(self) -> Audit:
        return Audit("licensing", "license_limits", "", self.actor, "limits updated",
                     {"plan": self.plan, "limits": self.limits})


@event
@dataclass(frozen=True)
class ConsoleUserAdded(DomainEvent):
    type = "console.user_added"
    owner = "console"
    aggregate = "console_user"
    aggregate_field = "email"

    actor: str
    email: str
    roles: list[str]
    project_keys: list[str]

    def audit(self) -> Audit:
        return Audit("console", "user_add", "", self.actor, "Person added",
                     {"email": self.email, "roles": self.roles, "project_keys": self.project_keys})


@event
@dataclass(frozen=True)
class ConsoleUserUpdated(DomainEvent):
    type = "console.user_updated"
    owner = "console"
    aggregate = "console_user"
    aggregate_field = "email"

    actor: str
    email: str
    roles: list[str]
    project_keys: list[str]

    def audit(self) -> Audit:
        return Audit("console", "user_update", "", self.actor, "Person updated",
                     {"email": self.email, "roles": self.roles, "project_keys": self.project_keys})


@event
@dataclass(frozen=True)
class ConsoleUserDisabledChanged(DomainEvent):
    type = "console.user_disabled_changed"
    owner = "console"
    aggregate = "console_user"
    aggregate_field = "email"

    actor: str
    email: str
    disabled: bool

    def audit(self) -> Audit:
        return Audit("console", "user_disable" if self.disabled else "user_enable", "", self.actor,
                     "Person disabled" if self.disabled else "Person enabled", {"email": self.email})
