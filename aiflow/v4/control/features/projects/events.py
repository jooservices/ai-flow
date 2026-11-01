"""Domain events for successful GitHub connection and environment writes in projects routes."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class GitHubRepositorySnapshotRecorded(DomainEvent):
    type = "github.repository_snapshot_recorded"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    count: int
    source: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "repository_snapshot", "", self.actor,
                     "GitHub installation repositories recorded", {"count": self.count, "source": self.source})


@event
@dataclass(frozen=True)
class GitHubRepositoryAccessChanged(DomainEvent):
    type = "github.repository_access_changed"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    added: list[str]
    removed: list[str]
    source: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "repositories_changed", "", self.actor,
                     "GitHub installation repository access changed",
                     {"added": self.added, "removed": self.removed, "source": self.source})


@event
@dataclass(frozen=True)
class GitHubAppConnected(DomainEvent):
    type = "github.app_connected"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    app_id: str
    installation_id: int
    account: str
    repositories: int
    source: str
    connection: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "connect", "", self.actor, "GitHub App connected",
                     {"app_id": self.app_id, "installation_id": self.installation_id, "account": self.account,
                      "repositories": self.repositories, "source": self.source, "connection": self.connection})


@event
@dataclass(frozen=True)
class GitHubTokenConnectionAdded(DomainEvent):
    type = "github.token_connection_added"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    connection: str
    account: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "add_token_connection", "", self.actor, "access token connection added",
                     {"connection": self.connection, "account": self.account})


@event
@dataclass(frozen=True)
class GitHubTokenConnectionReplaced(DomainEvent):
    type = "github.token_connection_replaced"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    connection: str
    account: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "replace_token_connection", "", self.actor, "access token replaced",
                     {"connection": self.connection, "account": self.account})


@event
@dataclass(frozen=True)
class GitHubConnectionRemoved(DomainEvent):
    type = "github.connection_removed"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    connection: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "remove_connection", "", self.actor, "connection removed",
                     {"connection": self.connection})


@event
@dataclass(frozen=True)
class GitHubManifestStarted(DomainEvent):
    type = "github.manifest_started"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    organization: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "manifest_start", "", self.actor, "GitHub App manifest started",
                     {"organization": self.organization})


@event
@dataclass(frozen=True)
class GitHubManifestCreated(DomainEvent):
    type = "github.manifest_created"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    app_id: str
    organization: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "manifest_created", "", self.actor, "GitHub App created",
                     {"app_id": self.app_id, "organization": self.organization})


@event
@dataclass(frozen=True)
class GitHubAppImportVerified(DomainEvent):
    type = "github.app_import_verified"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    app_id: int
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "import_app", "", self.actor, "GitHub App credentials verified",
                     {"app_id": self.app_id})


@event
@dataclass(frozen=True)
class GitHubAppInstallOpened(DomainEvent):
    type = "github.app_install_opened"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    app_id: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "open_app_install", "", self.actor, "GitHub App installation opened",
                     {"app_id": self.app_id})


@event
@dataclass(frozen=True)
class GitHubInstallationSettingsOpened(DomainEvent):
    type = "github.installation_settings_opened"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    app_id: str
    installation_id: int
    account: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "open_installation_settings", "", self.actor,
                     "GitHub repository settings opened",
                     {"app_id": self.app_id, "installation_id": self.installation_id, "account": self.account})


@event
@dataclass(frozen=True)
class GitHubConnectionAssigned(DomainEvent):
    type = "github.connection_assigned"
    owner = "projects"
    aggregate = "github"
    aggregate_field = "scope"

    actor: str
    repo: str
    connection: str
    project: str
    scope: str = "integration"

    def audit(self) -> Audit:
        return Audit("github", "assign_connection", "", self.actor,
                     f"{self.repo} uses connection {self.connection}",
                     {"repo": self.repo, "connection": self.connection, "project": self.project})


@event
@dataclass(frozen=True)
class EnvironmentRemovalsRecorded(DomainEvent):
    type = "environment.removals_recorded"
    owner = "projects"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    removed: int
    tickets: int
    needs: int

    def audit(self) -> Audit:
        return Audit("environment", "removals", self.ticket, self.actor,
                     f"{self.removed} removed, {self.tickets} environment ticket(s)",
                     {"needs": self.needs}, self.run_id, self.fence)
