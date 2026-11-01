"""Shared state and early rules for deciding whether a worker may receive tickets."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import HTTPException

from ... import role_health, shared_accounts, system_check
from ... import dev_history
from ....core import services, toolchains
from . import constants


@dataclass
class Scope:
    """Values and rules shared by every stage of a work eligibility check."""

    ctx: Any
    deps: Any
    repo: Any
    account: Any
    node: str
    node_row: dict
    projects: list[str]
    roles: set[str]
    ticket_key: str | None
    account_id: str
    missing_container_runtime: bool
    held: set[str] = field(default_factory=set)
    role_modes: dict[str, str] = field(default_factory=dict)
    quota_at_risk: bool = False
    scoped_key: str = ""
    scoped_tickets_key: str = ""
    scoped_ticket: tuple[str, ...] = ()
    backoff_sql: str | None = None
    intake_backoff_sql: str | None = None
    now: Callable | None = None
    project_priority: dict[str, int] = field(default_factory=dict)
    ticket_priority: dict[str, int] = field(default_factory=dict)
    gap_checker: Callable | None = None
    runtime_tool: str | None = None

    def __post_init__(self) -> None:
        """Prepare the optional ticket SQL fragments and late-bound default helpers."""
        self.scoped_key = " AND key=%s" if self.ticket_key is not None else ""
        self.scoped_tickets_key = " AND tickets.key=%s" if self.ticket_key is not None else ""
        self.scoped_ticket = (self.ticket_key,) if self.ticket_key is not None else ()
        if self.backoff_sql is None:
            self.backoff_sql = constants.BACKOFF_SQL
        if self.intake_backoff_sql is None:
            self.intake_backoff_sql = constants.INTAKE_BACKOFF_SQL
        if self.now is None:
            self.now = constants.utcnow
        if self.gap_checker is None:
            self.gap_checker = toolchains.toolchain_gaps
        if self.runtime_tool is None:
            self.runtime_tool = toolchains.CONTAINER_RUNTIME_TOOL

    def backoff_rows(self, step: str) -> list[dict]:
        """Load recent run failures, scoped to the requested ticket when revalidating a claim."""
        query = self.backoff_sql
        params = {"step": step}
        if self.ticket_key is not None:
            marker = "AND outcome IN ('failed', 'timeout', 'lost', 'no_changes')"
            query = query.replace(marker, marker + "\n    AND ticket_key = %(ticket_key)s", 1)
            params["ticket_key"] = self.ticket_key
        return self.ctx.db.all(query, params)

    def intake_backoff_rows(self, step: str) -> list[dict]:
        """Load recent intake failures, scoped to the requested ticket when revalidating a claim."""
        query = self.intake_backoff_sql
        params = {"step": step}
        if self.ticket_key is not None:
            marker = "ORDER BY ticket_key, started_at DESC"
            query = query.replace(marker, "AND ticket_key = %(ticket_key)s\n" + marker, 1)
            params["ticket_key"] = self.ticket_key
        return self.ctx.db.all(query, params)

    def allowed(self, role: str, ticket: dict) -> bool:
        """Hold a role-off account back except for its single unresolved probation ticket."""
        return (self.role_modes.get(role) == "on" or
                (self.role_modes.get(role) == "probation" and role_health.probation_ticket(ticket)))

    def probation(self, role: str) -> bool:
        """Mark a candidate when its account is using a role's probation slot."""
        return self.role_modes.get(role) == "probation"

    def needs_missing_container_runtime(self, ticket: dict, step: str) -> bool:
        """Hold container work back when this node has not reported the required runtime."""
        project = self.repo.projects.get(str(ticket.get("project") or ""))
        if not project or not self.missing_container_runtime:
            return False
        selected = project.settings_for_ticket({"key": ticket.get("key"), "type": ticket.get("type"),
                                                "epic": self.deps.ticket_epic(ticket)})
        # Verify and rework restore the repository settings captured by the development run. Gate on those
        # same settings so a later project remap cannot offer container work to a node without the runtime.
        if step in {"dev", "verify", "qa", "triage"}:
            prior_dev = dev_history.delivered_or_last(self.ctx.db, str(ticket.get("key") or ""))
            details = (prior_dev or {}).get("details") or {}
            prior_repository = str(details.get("repository") or "")
            if prior_repository:
                prior_settings = details.get("repository_settings")
                selected = (dict(prior_settings) if isinstance(prior_settings, dict)
                            else project.settings_for_repo(prior_repository))
        return bool(selected.get("containers"))

    def explicit_only_blocks(self, ticket: dict) -> bool:
        """Hold a ticket back when an explicit-only account is not named by its provider label."""
        return self.account.explicit_only and f"ai-{self.account.provider}" not in ticket["labels"]


def account_may_work(ctx, deps, acc, node: str, account_id: str | None = None) -> bool:
    """Hold an account back when it is unassigned, paused, cooling, or at its session limit."""
    if not acc or not acc.runs_on(node):
        denied_account = account_id if account_id is not None else getattr(acc, "id", "unknown")
        raise HTTPException(403, f"account {denied_account} does not run on {node}")
    if acc.paused or deps.account_master_paused(acc.id):
        return False
    if ctx.leases.cooling(acc.id):
        return False
    return bool(shared_accounts.may_take(ctx.db, acc, node)[0])


def online_node_row(ctx, node: str) -> dict | None:
    """Hold all work back until the node has an online status and a fresh heartbeat."""
    return ctx.db.one("SELECT projects,dynamic,tools,metrics,readiness,accepting_work FROM nodes WHERE name=%s"
                      " AND removed_at IS NULL AND status='online' AND last_heartbeat IS NOT NULL"
                      " AND last_heartbeat > now() - make_interval(secs => %s)",
                      (node, shared_accounts.NODE_ONLINE_S))


def node_accepts_work(node_row: dict) -> bool:
    """Hold work back when the latest node heartbeat says the agent is not accepting work."""
    return bool(node_row.get("accepting_work", True))


def open_projects(ctx, repo, node: str, node_row: dict, account_id: str, *, gap_checker: Callable | None = None,
                  service_gap_checker: Callable | None = None, runtime_tool: str | None = None
                  ) -> tuple[list[str], bool]:
    """Hold projects back for pause, parallel, tool, service, or readiness restrictions."""
    gaps = gap_checker or toolchains.toolchain_gaps
    service_gaps = service_gap_checker or services.service_gaps
    container_tool = runtime_tool or toolchains.CONTAINER_RUNTIME_TOOL
    projects = repo.projects_for_node(node, node_row.get("projects") or [],
                                      node_row.get("dynamic", False))
    projects = [key for key in projects if not repo.projects[key].paused]
    limited_projects = [key for key in projects if repo.projects[key].parallel_limit]
    if limited_projects:
        running_by_project = {row["project"]: int(row["count"]) for row in
                              ctx.db.all("SELECT project,count(*) AS count FROM runs"
                                         " WHERE status='running' AND project=ANY(%s) GROUP BY project",
                                         (limited_projects,))}
        projects = [key for key in projects if key not in limited_projects or
                    running_by_project.get(key, 0) < repo.projects[key].parallel_limit]
    node_tools = node_row.get("tools") or {}
    missing_container_runtime = bool(gaps(node_tools, [container_tool], repo.versions))
    # A declared stack is offered only after this node reports every pinned tool healthy.
    # Filtering here keeps another project on the same node available while a stack install is pending.
    projects = [p for p in projects if not (repo.projects[p].stacks or repo.projects[p].uses_containers)
                or not gaps(node_tools,
                            [tool for tool in repo.projects[p].tools if tool != container_tool],
                            repo.versions, repo.projects[p].stacks,
                            require_pins=bool(repo.projects[p].stacks))]
    reported_services = (node_row.get("metrics") or {}).get("services")
    projects = [p for p in projects
                if not service_gaps(reported_services, repo.projects[p].required_services)]
    projects = [p for p in projects if not system_check.blocking_reasons(node_row.get("readiness"), p, account_id)]
    return projects, missing_container_runtime
