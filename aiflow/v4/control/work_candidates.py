"""Role-specific candidate rules for the control work-offering pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException

from ..core.services import service_gaps
from ..core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from . import environment as environment_reports, hotfiles, role_health, tooling, verify_retry


@dataclass
class CandidateContext:
    ctx: Any
    repo: Any
    account: Any
    node: str
    node_row: dict
    projects: list[str]
    missing_container_runtime: bool
    roles: set[str]
    modes: dict[str, str]
    held: set[str]
    priorities: dict[str, int]
    quota_at_risk: bool
    capacity_routing: bool
    verify_retry_guard: bool
    last_dev: Callable
    ticket_epic: Callable
    backoff: Callable[[str], set[str]]
    intake_backoff: Callable[[str], set[str]]
    quota_peers: Callable
    peer_compatible: Callable
    peer_offers: Callable
    capacity_route: Callable
    entitled_candidates: Callable
    releases: Any
    license_error: Any


def available_projects(ctx, repo, node: str, node_row: dict) -> list[str]:
    projects = repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False))
    projects = _active_projects(ctx, repo, projects)
    return _ready_projects(repo, node_row, projects)


def resolve_account(ctx, account_id: str, node: str):
    repo = ctx.repo()
    account = repo.account(account_id)
    if not account or not account.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    return repo, account


def _active_projects(ctx, repo, projects: list[str]) -> list[str]:
    projects = [key for key in projects if key in repo.projects and not repo.projects[key].paused]
    running = {row["project"]: int(row["count"]) for row in ctx.db.all(
        "SELECT project,count(*) AS count FROM runs WHERE status='running' GROUP BY project")}
    return [key for key in projects if _within_parallel_limit(repo.projects[key], running)]


def _within_parallel_limit(project, running: dict[str, int]) -> bool:
    return not project.parallel_limit or running.get(project.key, 0) < project.parallel_limit


def _ready_projects(repo, node_row: dict, projects: list[str]) -> list[str]:
    node_tools = node_row.get("tools") or {}
    services = (node_row.get("metrics") or {}).get("services") or {}
    return [key for key in projects if _stack_ready(repo, node_tools, key)
            and not service_gaps(services, repo.projects[key].required_services)]


def _stack_ready(repo, node_tools: dict, key: str) -> bool:
    project = repo.projects[key]
    if not (project.stacks or project.uses_containers):
        return True
    required = [tool for tool in project.tools if tool != CONTAINER_RUNTIME_TOOL]
    return not toolchain_gaps(node_tools, required, repo.versions, project.stacks,
                              require_pins=bool(project.stacks))


def collect(state: CandidateContext) -> list[dict]:
    if not state.projects:
        return []
    out: list[dict] = []
    if "verify" in state.roles:
        out.extend(_verify(state))
    if "release" in state.roles:
        out.extend(_release(state))
    if "master" in state.roles:
        out.extend(_triage(state))
    if "intake_refiner" in state.roles:
        out.extend(_intake(state))
        out.extend(_impact(state))
    out.sort(key=lambda ticket: (state.priorities.get(ticket["project"], 5), ticket["jira_created"] or _now()))
    if "dev" in state.roles:
        out.extend(_development(state))
    out.sort(key=lambda ticket: "flow-bug" not in (ticket.get("labels") or []))
    if state.quota_at_risk:
        out = _exclude_peer_offers(state, out)
    if state.capacity_routing and out:
        out = state.capacity_route(state.account.id, state.node, out, state.verify_retry_guard)
    return out


def _verify(state: CandidateContext) -> list[dict]:
    return verify_retry.candidates(
        state.ctx, state.projects, state.account, state.held,
        lambda peer, machine: verify_retry.final_offers(
            state.ctx, peer, machine, state.entitled_candidates, state.peer_compatible,
            state.releases, state.license_error),
        lambda ticket: not _needs_missing_runtime(state, ticket, "verify"),
        lambda role, ticket: _allowed(state, role, ticket),
        lambda role: _probation(state, role), retry_guard=state.verify_retry_guard)


def _release(state: CandidateContext) -> list[dict]:
    cooling = state.backoff("post_merge")
    pending = [item for key in state.projects for item in environment_reports.pending_post_merge(state.ctx, key)]
    out = []
    for waiting in pending:
        ticket = state.ctx.db.one("SELECT * FROM tickets WHERE key=%s", (waiting["ticket"],))
        if not ticket or verify_retry.is_epic(ticket) or ticket["key"] in state.held or ticket["key"] in cooling:
            continue
        if state.account.explicit_only and f"ai-{state.account.provider}" not in ticket["labels"]:
            continue
        if not _allowed(state, "release", ticket):
            continue
        out.append({**ticket, "step": "post_merge", "probation": _probation(state, "release")})
    return out


def _triage(state: CandidateContext) -> list[dict]:
    cooling = state.backoff("triage")
    tickets = state.ctx.db.all(
        "SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'"
        " AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))"
        " AND lower(coalesce(type,'')) <> 'epic' AND EXISTS (SELECT 1 FROM escalations e"
        " WHERE e.ticket_key = tickets.key AND e.resolution = 'open')", (state.projects,))
    out = []
    for ticket in tickets:
        if _needs_missing_runtime(state, ticket, "triage") or ticket["key"] in state.held:
            continue
        if ticket["key"] in cooling:
            continue
        dev = state.last_dev(ticket["key"])
        if dev and dev["account_id"] == state.account.id:
            continue
        if state.account.explicit_only and f"ai-{state.account.provider}" not in ticket["labels"]:
            continue
        if not _allowed(state, "master", ticket):
            continue
        out.append({**ticket, "step": "triage", "probation": _probation(state, "master")})
    return out


def _intake(state: CandidateContext) -> list[dict]:
    waiting = state.intake_backoff("intake")
    tickets = state.ctx.db.all(
        "SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'"
        " AND NOT ('aiflow-hold' = ANY(labels)) AND lower(coalesce(type,'')) <> 'epic'"
        " ORDER BY jira_created NULLS LAST, key", (state.projects,))
    return _simple_intake(state, tickets, waiting, "intake")


def _impact(state: CandidateContext) -> list[dict]:
    waiting = state.intake_backoff("impact")
    tickets = state.ctx.db.all(
        "SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'"
        " AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
        " AND lower(coalesce(type,'')) <> 'epic' AND NOT EXISTS (SELECT 1 FROM runs r"
        " WHERE r.ticket_key = tickets.key AND r.step = 'impact' AND r.outcome = 'assessed')"
        " ORDER BY jira_created NULLS LAST, key", (state.projects,))
    return _simple_intake(state, tickets, waiting, "impact")


def _simple_intake(state: CandidateContext, tickets: list[dict], waiting: set[str], step: str) -> list[dict]:
    out = []
    for ticket in tickets:
        if _needs_missing_runtime(state, ticket, step) or ticket["key"] in state.held:
            continue
        if ticket["key"] in waiting:
            continue
        if state.account.explicit_only and f"ai-{state.account.provider}" not in ticket["labels"]:
            continue
        if not _allowed(state, "intake_refiner", ticket):
            continue
        out.append({**ticket, "step": step, "probation": _probation(state, "intake_refiner")})
    return out


def _development(state: CandidateContext) -> list[dict]:
    done = {row["key"] for row in state.ctx.db.all("SELECT key FROM tickets WHERE status = 'Done'")}
    cooling = state.backoff("dev")
    parked = tooling.parked_tickets(state.ctx, state.node_row) | environment_reports.parked_tickets(state.ctx)
    footprints: dict[str, dict] = {}

    def footprint(ticket: dict) -> dict:
        key = ticket["key"]
        if key not in footprints:
            footprints[key] = hotfiles.predict(state.ctx.db, ticket, state.ticket_epic(ticket))
        return footprints[key]

    flying = None
    tickets = state.ctx.db.all(
        "SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'To Do'"
        " AND 'aiflow-ready' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
        " AND lower(coalesce(type,'')) <> 'epic'", (state.projects,))
    out = []
    for ticket in tickets:
        if _skip_development(state, ticket, done, cooling, parked):
            continue
        if flying is None:
            flying = hotfiles.in_flight_hot_files(state.ctx.db, footprint)
        prediction = footprint(ticket)
        blocked = hotfiles.blocking_ticket(prediction, {key: value for key, value in flying.items()
                                                        if key != ticket["key"]},
                                           slots=state.ctx.settings.hot_file_slots)
        if blocked:
            hotfiles.mark_blocked(state.ctx.db, ticket["key"])
            continue
        hotfiles.clear_blocked(state.ctx.db, ticket["key"])
        out.append({**ticket, "step": "dev", "probation": _probation(state, "dev")})
    out.sort(key=lambda ticket: (state.priorities.get(ticket["project"], 5),
                                 "rework" not in ticket["labels"],
                                 _priority(ticket.get("priority")), ticket["jira_created"] or _now()))
    return out


def _skip_development(state: CandidateContext, ticket: dict, done: set[str], cooling: set[str],
                      parked: set[str]) -> bool:
    if _needs_missing_runtime(state, ticket, "dev"):
        return True
    if ticket["key"] in state.held or ticket["key"] in cooling or ticket["key"] in parked:
        return True
    if any(blocker not in done for blocker in ticket["blocked_by"]):
        return True
    labels = set(ticket["labels"])
    if "tier-senior" in labels and state.account.tier != "senior":
        return True
    if state.account.explicit_only and f"ai-{state.account.provider}" not in labels:
        return True
    return not _allowed(state, "dev", ticket)


def _needs_missing_runtime(state: CandidateContext, ticket: dict, step: str) -> bool:
    if not state.missing_container_runtime:
        return False
    project = state.repo.projects.get(str(ticket.get("project") or ""))
    if not project:
        return False
    return _selected_settings_use_containers(state, ticket, project, step)


def _selected_settings_use_containers(state: CandidateContext, ticket: dict, project, step: str) -> bool:
    selected = project.settings_for_ticket({"key": ticket.get("key"), "type": ticket.get("type"),
                                            "epic": state.ticket_epic(ticket)})
    if step in {"dev", "verify", "qa", "triage"}:
        prior = state.last_dev(str(ticket.get("key") or ""), done=True) or state.last_dev(
            str(ticket.get("key") or ""))
        details = (prior or {}).get("details") or {}
        repository = str(details.get("repository") or "")
        if repository:
            saved = details.get("repository_settings")
            selected = (dict(saved) if isinstance(saved, dict) else project.settings_for_repo(repository))
    return bool(selected.get("containers"))


def _exclude_peer_offers(state: CandidateContext, offered: list[dict]) -> list[dict]:
    available: set[tuple[str, str]] = set()
    for peer in state.quota_peers(state.account.id, state.roles):
        for node in peer["nodes"]:
            if not state.peer_compatible(state.ctx, node, state.releases)["compatible"]:
                continue
            available.update((ticket["key"], ticket["step"])
                             for ticket in state.peer_offers(peer["id"], node, state.verify_retry_guard))
    return [ticket for ticket in offered if (ticket["key"], ticket["step"]) not in available]


def _allowed(state: CandidateContext, role: str, ticket: dict) -> bool:
    mode = state.modes.get(role)
    return mode == "on" or (mode == "probation" and role_health.probation_ticket(ticket))


def _probation(state: CandidateContext, role: str) -> bool:
    return state.modes.get(role) == "probation"


def _priority(value: str | None) -> int:
    return {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}.get(value, 2)


def _now():
    return datetime.now(timezone.utc)
