"""Domain events published by the fleet routes when they write audit rows."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class NodeJoinCommandCreated(DomainEvent):
    type = "node.join_command_created"
    owner = "fleet"
    aggregate = "node"
    aggregate_field = "node"

    node: str
    actor: str
    expires_at: str
    role: str
    project: str

    def audit(self) -> Audit:
        return Audit("node", "join_command", self.node, self.actor, "one-time join command created",
                     {"expires_at": self.expires_at, "role": self.role, "project": self.project,
                      "ssh_password_saved": False})


@event
@dataclass(frozen=True)
class NodeWorkAcceptanceChanged(DomainEvent):
    type = "node.work_acceptance_changed"
    owner = "fleet"
    aggregate = "node"
    aggregate_field = "node"

    node: str
    actor: str
    accepting_work: bool

    def audit(self) -> Audit:
        action = "resume_work" if self.accepting_work else "stop_taking_work"
        result = "accepting new work" if self.accepting_work else "stopping new work"
        return Audit("node", action, self.node, self.actor, result, {})


@event
@dataclass(frozen=True)
class CheckProbeTokenIssued(DomainEvent):
    type = "github.check_probe_token_issued"
    owner = "fleet"
    aggregate = "node"
    aggregate_field = "node"

    node: str
    repo: str
    project: str
    connection: str
    expires: str
    transport: str

    def audit(self) -> Audit:
        payload = {"repo": self.repo, "project": self.project, "connection": self.connection,
                   "expires": self.expires}
        if self.transport == "control":
            payload["transport"] = "control"
            return Audit("github", "check_probe_token", "", self.node, "read-only relay capability issued", payload)
        return Audit("github", "check_probe_token", "", self.node, "issued", payload)
