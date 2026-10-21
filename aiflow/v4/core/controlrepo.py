"""The control repo (jooservices/aiflow-control): versions, policy, profiles, accounts, nodes, projects.

Files are plain YAML; this module loads one checkout into a snapshot object and answers the questions the
control plane and the nodes ask (which accounts run on a node, which projects a node serves, a project's
repo and branches, the policy of a project).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Account:
    id: str
    provider: str
    node: str
    tier: str = "middle"
    roles: list[str] = field(default_factory=list)
    explicit_only: bool = False


@dataclass
class Project:
    key: str
    repo: str
    base: str = "develop"
    hotfix_base: str = "master"
    stack: str = ""
    databases: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    environments: dict[str, Any] = field(default_factory=dict)
    merge: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def repo_name(self) -> str:
        return self.repo.split("/", 1)[-1]

    def work_minutes(self) -> dict[str, int]:
        wm = (self.limits or {}).get("work_minutes") or {}
        return {"target": int(wm.get("target", 30)), "warn": int(wm.get("warn", 45)), "hard": int(wm.get("hard", 60))}


@dataclass
class Node:
    name: str
    ip: str = ""
    role: str = "worker"
    slots: int = 1
    capabilities: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)


@dataclass
class ControlRepo:
    root: Path
    versions: dict[str, Any]
    policy: dict[str, Any]
    profiles: dict[str, Any]
    accounts: list[Account]
    nodes: dict[str, Node]
    projects: dict[str, Project]
    ref: str = ""

    @classmethod
    def load(cls, root: str | Path) -> "ControlRepo":
        r = Path(root)

        def y(rel: str, default: Any) -> Any:
            p = r / rel
            return (yaml.safe_load(p.read_text(encoding="utf-8")) or default) if p.is_file() else default

        accounts = [Account(**{k: v for k, v in a.items() if k in Account.__dataclass_fields__})
                    for a in (y("accounts.yaml", {}).get("accounts") or [])]
        nodes: dict[str, Node] = {}
        for p in sorted((r / "nodes").glob("*.yaml")):
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            n = Node(**{k: v for k, v in d.items() if k in Node.__dataclass_fields__})
            nodes[n.name] = n
        projects: dict[str, Project] = {}
        for p in sorted((r / "projects").glob("*.yaml")):
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            pr = Project(**{k: v for k, v in d.items() if k in Project.__dataclass_fields__})
            projects[pr.key] = pr
        ref = ""
        head = r / ".git" / "HEAD"
        if head.is_file():
            ref = head.read_text().strip()
        return cls(root=r, versions=y("versions.lock", {}), policy=y("policy/default.yaml", {}),
                   profiles=y("profiles.yaml", {}), accounts=accounts, nodes=nodes, projects=projects, ref=ref)

    def accounts_on(self, node: str) -> list[Account]:
        return [a for a in self.accounts if a.node == node]

    def account(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def model_for(self, provider: str, tier: str) -> tuple[str, str]:
        """(model, effort) of a provider at a tier; empty strings mean the CLI default."""
        p = (self.profiles.get("providers") or {}).get(provider) or {}
        models = p.get("models") or {}
        effort = (p.get("effort") or {}).get(tier, "")
        return str(models.get(tier) or models.get("middle") or ""), str(effort or "")

    def policy_for(self, project: str | None) -> dict[str, Any]:
        base = dict(self.policy or {})
        if project and project in self.projects and self.projects[project].policy:
            for k, v in self.projects[project].policy.items():
                base[k] = v
        return base
