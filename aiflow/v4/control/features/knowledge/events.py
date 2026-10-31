"""Domain events published by the knowledge feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class KnowledgeItemSaved(DomainEvent):
    type = "knowledge.item_saved"
    owner = "knowledge"
    aggregate = "knowledge_resource"
    aggregate_field = "resource"

    resource: str
    actor: str
    item_id: str
    kind: str
    project: str
    scope: str

    def audit(self) -> Audit:
        return Audit("knowledge", "item_save", self.item_id, self.actor, "saved",
                     {"kind": self.kind, "project": self.project, "scope": self.scope})


@event
@dataclass(frozen=True)
class KnowledgeItemModerated(DomainEvent):
    type = "knowledge.item_moderated"
    owner = "knowledge"
    aggregate = "knowledge_resource"
    aggregate_field = "resource"

    resource: str
    actor: str
    item_id: int
    action: str

    def audit(self) -> Audit:
        return Audit("knowledge", "item_" + self.action, str(self.item_id), self.actor, "updated",
                     {"item_id": self.item_id})


@event
@dataclass(frozen=True)
class PolicyProposed(DomainEvent):
    type = "knowledge.policy_proposed"
    owner = "knowledge"
    aggregate = "knowledge_resource"
    aggregate_field = "resource"

    resource: str
    actor: str
    kind: str
    resource_id: str
    branch: str
    pr_number: int
    pr_url: str
    commit: str

    def audit(self) -> Audit:
        return Audit("aiflow-control", "policy-pr", "", self.actor, self.pr_url,
                     {"type": self.kind, "id": self.resource_id, "branch": self.branch,
                      "pr": self.pr_number, "commit": self.commit})


@event
@dataclass(frozen=True)
class ResourceCommitted(DomainEvent):
    type = "knowledge.resource_committed"
    owner = "knowledge"
    aggregate = "knowledge_resource"
    aggregate_field = "resource"

    resource: str
    actor: str
    kind: str
    resource_id: str
    resource_version: int
    path: str
    commit: str

    def audit(self) -> Audit:
        return Audit("aiflow-control", "resource-commit", "", self.actor, self.commit,
                     {"type": self.kind, "id": self.resource_id, "version": self.resource_version,
                      "path": self.path})


@event
@dataclass(frozen=True)
class PolicyMerged(DomainEvent):
    type = "knowledge.policy_merged"
    owner = "knowledge"
    aggregate = "knowledge_resource"
    aggregate_field = "resource"

    resource: str
    actor: str
    resource_id: str
    pr_number: int
    resource_version: int
    commit: str

    def audit(self) -> Audit:
        return Audit("aiflow-control", "policy-merge", "", self.actor, self.commit,
                     {"id": self.resource_id, "pr": self.pr_number, "version": self.resource_version})
