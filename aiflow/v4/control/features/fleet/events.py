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


@event
@dataclass(frozen=True)
class NodeJoined(DomainEvent):
    type = "node.agent_joined"
    owner = "fleet"
    aggregate = "node"
    aggregate_field = "node"

    node: str
    ip_present: bool
    role: str
    project: str

    def audit(self) -> Audit:
        return Audit("node", "join", self.node, "node-agent", "node joined",
                     {"ip_present": self.ip_present, "role": self.role, "project": self.project})


@event
@dataclass(frozen=True)
class AiAccountAdded(DomainEvent):
    type = "ai_account.added"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    node: str
    provider: str
    name: str
    sessions: int
    jobs: list[str]

    def audit(self) -> Audit:
        return Audit("ai-account", "add", self.account, self.actor, "account added; sign-in pending",
                     {"node": self.node, "provider": self.provider, "name": self.name,
                      "sessions": self.sessions, "jobs": self.jobs})


@event
@dataclass(frozen=True)
class AiAccountManagementChanged(DomainEvent):
    type = "ai_account.management_changed"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    action: str
    sessions: int
    jobs: list[str]
    paused: bool

    def audit(self) -> Audit:
        return Audit("ai-account", self.action, self.account, self.actor, "updated",
                     {"sessions": self.sessions, "jobs": self.jobs, "paused": self.paused})


@event
@dataclass(frozen=True)
class AiAccountSignInRestarted(DomainEvent):
    type = "ai_account.sign_in_restarted"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    provider: str
    node: str

    def audit(self) -> Audit:
        return Audit("ai-account", "sign_in_again", self.account, self.actor,
                     "provider sign-in queued on the machine", {"provider": self.provider, "node": self.node})


@event
@dataclass(frozen=True)
class AiAccountSignInCompleted(DomainEvent):
    type = "ai_account.sign_in_completed"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    node: str
    provider: str

    def audit(self) -> Audit:
        return Audit("ai-account", "sign_in_complete", self.account, self.actor,
                     "provider account check succeeded", {"node": self.node, "provider": self.provider})


@event
@dataclass(frozen=True)
class AiAccountRemoved(DomainEvent):
    type = "ai_account.removed"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    node: str

    def audit(self) -> Audit:
        return Audit("ai-account", "remove", self.account, self.actor, "account removed from machine",
                     {"node": self.node})


@event
@dataclass(frozen=True)
class NodeRemoved(DomainEvent):
    type = "node.managed_removed"
    owner = "fleet"
    aggregate = "node"
    aggregate_field = "node"

    node: str
    actor: str

    def audit(self) -> Audit:
        return Audit("node", "remove", self.node, self.actor, "machine removed", {})


@event
@dataclass(frozen=True)
class NodeOperationQueued(DomainEvent):
    type = "node.operation_queued"
    owner = "fleet"
    aggregate = "node"
    aggregate_field = "node"

    node: str
    actor: str
    action: str
    operation_id: str
    components: list[str]
    service_count: int

    def audit(self) -> Audit:
        return Audit("node", self.action, self.node, self.actor, "queued",
                     {"operation_id": self.operation_id, "components": self.components,
                      "service_count": self.service_count}, run_id=self.operation_id, fence=None)


@event
@dataclass(frozen=True)
class SharedAccountCreated(DomainEvent):
    type = "ai_account.created"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str

    def audit(self) -> Audit:
        return Audit("account", "create", self.account, self.actor, "created")


@event
@dataclass(frozen=True)
class SharedAccountSessionLimitSet(DomainEvent):
    type = "ai_account.session_limit_set"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    session_limit: int

    def audit(self) -> Audit:
        return Audit("account", "session_limit", self.account, self.actor, str(self.session_limit))


@event
@dataclass(frozen=True)
class SharedAccountMachineEnabled(DomainEvent):
    type = "ai_account.machine_enabled"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    node: str

    def audit(self) -> Audit:
        return Audit("account", "enable_machine", self.account, self.actor, self.node)


@event
@dataclass(frozen=True)
class SharedAccountSignInRetried(DomainEvent):
    type = "ai_account.sign_in_retried"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    node: str

    def audit(self) -> Audit:
        return Audit("account", "retry_signin", self.account, self.actor, self.node)


@event
@dataclass(frozen=True)
class SharedAccountMachineRemoved(DomainEvent):
    type = "ai_account.machine_removed"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    node: str
    account_removed: bool

    def audit(self) -> Audit:
        return Audit("account", "remove_machine", self.account, self.actor, self.node,
                     {"account_removed": self.account_removed})


@event
@dataclass(frozen=True)
class SharedAccountRemoved(DomainEvent):
    type = "ai_account.removed_everywhere"
    owner = "fleet"
    aggregate = "ai_account"
    aggregate_field = "account"

    account: str
    actor: str
    source_retained: bool

    def audit(self) -> Audit:
        return Audit("account", "remove", self.account, self.actor, "removed from all machines",
                     {"source_retained": self.source_retained})


@event
@dataclass(frozen=True)
class MachineLoadLimitsSaved(DomainEvent):
    type = "node.load_limits_saved"
    owner = "fleet"
    aggregate = "node"
    aggregate_field = "node"

    node: str
    actor: str
    machine: dict
    host: str | None
    host_cpu_load_per_cpu: float | None

    def audit(self) -> Audit:
        return Audit("machine-load", "limits", self.node, self.actor, "load limits saved",
                     {"machine": self.machine, "host": self.host,
                      "host_cpu_load_per_cpu": self.host_cpu_load_per_cpu})
