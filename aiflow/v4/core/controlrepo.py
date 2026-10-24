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

from .environment import EnvItem, items_of
from .services import normalize_services
from .toolchains import (BASE_TOOLS, CATALOG_TOOLS, CONTAINER_RUNTIME_TOOL, NODE_TOOLS, STACK_TOOLS, missing_tools,
                         tools_for_providers, tools_for_stack,
                         tools_for_stacks, tools_for_stacks_in_project, normalize_stacks)

__all__ = ["Account", "ControlRepo", "Node", "Project", "NODE_TOOLS", "STACK_TOOLS", "missing_tools",
           "tools_for_stack", "tools_for_stacks", "epic_for_ticket"]


def epic_for_ticket(ticket: dict[str, Any], tickets_by_key: dict[str, dict[str, Any]] | None = None) -> str:
    """Resolve a ticket's epic key through its parent chain, including subtasks below stories."""
    by_key = tickets_by_key or {}
    if ticket.get("epic") or ticket.get("epic_key"):
        return str(ticket.get("epic") or ticket.get("epic_key"))
    if ticket.get("type") == "Epic":
        return str(ticket.get("key") or "")
    parent = str(ticket.get("parent_key") or "")
    seen: set[str] = set()
    while parent and parent not in seen:
        seen.add(parent)
        item = by_key.get(parent)
        if not item:
            return parent
        if item.get("type") == "Epic":
            return parent
        parent = str(item.get("parent_key") or "")
    return ""


@dataclass
class Account:
    id: str
    provider: str
    node: str
    tier: str = "middle"
    roles: list[str] = field(default_factory=list)
    explicit_only: bool = False
    parallel: int = 1          # workers of this account on its node (sessions of one account can run side by side)
    session_limit: int = 0     # total concurrent sessions across all machines; 0 = `parallel`
    extra_machines: list[str] = field(default_factory=list)  # machines added beyond `node` (docs/v4/shared-accounts.md)
    paused: bool = False       # console-managed accounts stay assigned but do not receive new work

    @property
    def machines(self) -> list[str]:
        """Every machine the account is enabled on, its control-repo node first."""
        return list(dict.fromkeys(([self.node] if self.node else []) + list(self.extra_machines)))

    def runs_on(self, node: str) -> bool:
        return bool(node) and node in self.machines

    @property
    def sessions(self) -> int:
        """Concurrent sessions the account may have in total, however many machines use it."""
        return max(1, int(self.session_limit or self.parallel or 1))


@dataclass
class Project:
    key: str
    repo: str
    base: str = "develop"
    hotfix_base: str = "master"
    stack: str | list[str] = ""
    databases: list[str] = field(default_factory=list)
    services: list[str] | str = field(default_factory=list)  # test services every node prepares: postgres, redis
    checks: list[str] = field(default_factory=list)
    containers: bool = False
    release_check: list[str] = field(default_factory=list)
    tag_prefix: str = ""
    environments: dict[str, Any] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    environment: list[dict[str, Any]] | dict[str, Any] = field(default_factory=list)  # docs/v4/environment.md
    merge: dict[str, Any] = field(default_factory=dict)
    version_release: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    priority: int = 5          # lower first: workers serve the projects in this order
    paused: bool = False       # paused projects remain configured but do not sync or receive new claims
    nodes: list[str] = field(default_factory=list)  # optional node assignments for projects created in the console
    repositories: list[dict[str, Any]] = field(default_factory=list)
    epic_repositories: dict[str, str] = field(default_factory=dict)
    default_repo: str = ""  # retained on repository-specific project views

    @property
    def repo_name(self) -> str:
        return self.repo.split("/", 1)[-1]

    @staticmethod
    def _epic_key(ticket: dict[str, Any] | None) -> str:
        if not ticket:
            return ""
        return str(ticket.get("epic") or ticket.get("epic_key") or
                   (ticket.get("key") if ticket.get("type") == "Epic" else "") or
                   ticket.get("parent_key") or "")

    def repo_for(self, ticket: dict[str, Any] | None) -> str:
        """Select a ticket's repository by its resolved epic, falling back to the project default."""
        epic = self._epic_key(ticket)
        mapped = (self.epic_repositories or {}).get(epic, "") if epic else ""
        if mapped:
            return str(mapped)
        for item in self.repositories or []:
            if not isinstance(item, dict):
                continue
            if epic and epic in (item.get("epics") or []):
                return str(item.get("repo") or self.repo)
        return self.default_repo or self.repo

    def settings_for_repo(self, repo: str) -> dict[str, Any]:
        """Return repository settings with legacy project values as defaults."""
        selected = next((item for item in self.repositories or []
                         if isinstance(item, dict) and str(item.get("repo") or "").lower() == repo.lower()), {})
        repo_containers = selected.get("containers")
        return {"repo": repo or self.repo,
                "base": str(selected.get("base") or self.base),
                "checks": list(selected.get("checks") or self.checks),
                "stack": selected.get("stack") or self.stack,
                "containers": bool(self.containers if repo_containers is None else repo_containers)}

    def settings_for_ticket(self, ticket: dict[str, Any] | None) -> dict[str, Any]:
        return self.settings_for_repo(self.repo_for(ticket))

    @property
    def configured_repositories(self) -> list[str]:
        return list(dict.fromkeys([self.repo] + [str(item.get("repo") or "") for item in self.repositories
                                                   if isinstance(item, dict) and item.get("repo")]))

    def work_minutes(self) -> dict[str, int]:
        wm = (self.limits or {}).get("work_minutes") or {}
        return {"target": int(wm.get("target", 30)), "warn": int(wm.get("warn", 45)), "hard": int(wm.get("hard", 60))}

    @property
    def tools(self) -> list[str]:
        """The base worker tools plus the extra tools this project's stack needs."""
        out = tools_for_stacks_in_project(self.stacks)
        if self.uses_containers and CONTAINER_RUNTIME_TOOL not in out:
            out.append(CONTAINER_RUNTIME_TOOL)
        return out

    @property
    def uses_containers(self) -> bool:
        """Whether the project or any repository settings require the safe container runtime."""
        return bool(self.containers or any(isinstance(item, dict) and item.get("containers") is True
                                           for item in self.repositories or []))

    @property
    def stacks(self) -> list[str]:
        """Normalized stacks required by the defaults and every repository in this project."""
        values: list[str] = list(normalize_stacks(self.stack))
        for item in self.repositories or []:
            if isinstance(item, dict):
                values.extend(normalize_stacks(item.get("stack")))
        return list(dict.fromkeys(values))

    @property
    def environment_items(self) -> list[EnvItem]:
        """The valid items of the project's environment section (test services, URLs, databases, browsers, secrets)."""
        return items_of(self.environment)

    @property
    def required_services(self) -> list[str]:
        """Canonical test services the nodes must run healthy before taking work for this project."""
        return normalize_services(self.services)

    @property
    def parallel_limit(self) -> int:
        """Maximum number of simultaneous runs for this project; zero means unlimited."""
        try:
            return max(0, int((self.limits or {}).get("parallel", 0)))
        except (TypeError, ValueError):
            return 0


@dataclass
class Node:
    name: str
    ip: str = ""
    role: str = "worker"
    slots: int = 1
    capabilities: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    purpose: str = ""
    notes: str = ""
    location: str = ""
    tools: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    services: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ControlRepo:
    root: Path
    versions: dict[str, Any]
    policy: dict[str, Any]
    profiles: dict[str, Any]
    sa: dict[str, Any]
    accounts: list[Account]
    nodes: dict[str, Node]
    projects: dict[str, Project]
    policy_projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    policy_archived: bool = False
    ref: str = ""

    @classmethod
    def load(cls, root: str | Path) -> "ControlRepo":
        r = Path(root)

        def y(rel: str, default: Any) -> Any:
            p = r / rel
            return (yaml.safe_load(p.read_text(encoding="utf-8")) or default) if p.is_file() else default

        accounts = [Account(**{k: v for k, v in a.items() if k in Account.__dataclass_fields__})
                    for a in (y("accounts.yaml", {}).get("accounts") or [])]
        # A configured senior account is the default master on a fresh control repo. Keep an explicit
        # master assignment authoritative; this fallback only fills the role when nobody has it.
        if accounts and not any("master" in (a.roles or []) for a in accounts):
            senior = next((a for a in accounts if a.tier == "senior"), None)
            if senior:
                # Empty/omitted roles otherwise mean the normal dev and verify roles in the control API.
                # Materialize those defaults before adding master so that this default does not erase them.
                defaults = senior.roles or ["dev", "verify"]
                senior.roles = list(dict.fromkeys([*defaults, "master"]))
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
        policy_file = r / "policy/default.yaml"
        policy_archived = policy_file.is_file() and "# aiflow4-archived: true" in policy_file.read_text(encoding="utf-8").splitlines()[:3]
        policy_projects: dict[str, dict[str, Any]] = {}
        for p in sorted((r / "policy/projects").glob("*.yaml")):
            raw = p.read_text(encoding="utf-8")
            if "# aiflow4-archived: true" in raw.splitlines()[:3]:
                continue
            data = yaml.safe_load(raw) or {}
            if isinstance(data, dict):
                policy_projects[p.stem] = data
        return cls(root=r, versions=y("versions.lock", {}), policy={} if policy_archived else y("policy/default.yaml", {}),
                   profiles=y("profiles.yaml", {}), sa=y("sa.yaml", {}), accounts=accounts, nodes=nodes,
                   projects=projects, policy_projects=policy_projects, policy_archived=policy_archived, ref=ref)

    def accounts_on(self, node: str) -> list[Account]:
        return [a for a in self.accounts if a.runs_on(node)]

    def tools_for_node(self, node: str, projects: list[str] | None = None) -> list[str]:
        """Tools the node needs: the base tools, the CLIs of its accounts' providers and every served stack's tools."""
        out = list(BASE_TOOLS) + tools_for_providers(a.provider for a in self.accounts_on(node))
        n = self.nodes.get(node)
        installable = set(NODE_TOOLS).union(CATALOG_TOOLS, *(set(tools) for tools in STACK_TOOLS.values()))
        for tool in (n.tools if n else []):
            if tool in installable and tool not in out:
                out.append(tool)
        project_keys = projects if projects is not None else (n.projects if n else [])
        for p in project_keys:
            if p in self.projects:
                for tool in self.projects[p].tools:
                    if tool not in out:
                        out.append(tool)
        return out

    def projects_for_node(self, node: str, bound_projects: list[str], dynamic: bool = False) -> list[str]:
        """Return the projects this node serves, using YAML as authority for configured nodes."""
        configured = self.nodes.get(node)
        if configured:
            assigned = list(configured.projects)
            assigned.extend(key for key, project in self.projects.items() if node in project.nodes)
            return [key for key in dict.fromkeys(assigned)
                    if key in self.projects and not self.projects[key].paused]
        if dynamic:
            assigned = list(bound_projects)
            assigned.extend(key for key, project in self.projects.items() if node in project.nodes)
            return [key for key in dict.fromkeys(assigned)
                    if key in self.projects and not self.projects[key].paused]
        return []

    def provider_tools_for_node(self, node: str) -> list[str]:
        return tools_for_providers(a.provider for a in self.accounts_on(node))

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
        if project and project in self.policy_projects:
            base.update(self.policy_projects[project])
        if project and project in self.projects and self.projects[project].policy:
            for k, v in self.projects[project].policy.items():
                base[k] = v
        return base
