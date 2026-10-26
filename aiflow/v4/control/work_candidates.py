"""Role-specific candidate rules for the control work-offering pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException

from ..core.services import service_gaps
from ..core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from . import environment as environment_reports, hotfiles, role_health, tooling, verify_retry, work_eligibility
from .repositories.runs import RunsRepository
from .repositories.tickets import TicketsRepository


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
    runs_repo: RunsRepository
    tickets_repo: TicketsRepository
    quota_peers: Callable
    peer_compatible: Callable
    peer_offers: Callable
    capacity_route: Callable
    entitled_candidates: Callable
    releases: Any
    license_error: Any


@dataclass(frozen=True)
class CandidateStrategy:
    name: str
    role: str
    phase: int
    collect: Callable[[CandidateContext], list[dict]]


_ROLE_STRATEGIES: dict[str, CandidateStrategy] = {}
_STEP_ROLE = {"dev": "dev", "verify": "verify", "triage": "master", "intake": "intake_refiner",
              "impact": "intake_refiner", "post_merge": "release"}
_RULES_BY_STEP = {
    "post_merge": ("not_epic", "not_held", "not_cooling", "provider_label", "role_enabled"),
    "triage": ("not_epic", "not_held", "not_cooling", "runtime_ready", "not_same_dev", "provider_label",
               "role_enabled"),
    "intake": ("not_epic", "not_held", "not_cooling", "runtime_ready", "provider_label", "role_enabled"),
    "impact": ("not_epic", "not_held", "not_cooling", "runtime_ready", "provider_label", "role_enabled"),
    "dev": ("not_epic", "not_held", "not_cooling", "runtime_ready", "not_parked", "blockers_done",
            "senior_tier", "provider_label", "role_enabled", "hot_file_capacity"),
}


def register_strategy(name: str, role: str, phase: int, collect: Callable[[CandidateContext], list[dict]]) -> None:
    """Register a role strategy by name; collection order is controlled by its phase."""
    if name in _ROLE_STRATEGIES:
        raise ValueError(f"candidate strategy is already registered: {name}")
    _ROLE_STRATEGIES[name] = CandidateStrategy(name, role, phase, collect)


def _register_rules() -> None:
    for name, check in (("not_epic", _reject_epic), ("not_held", _reject_held),
                        ("not_cooling", _reject_cooling), ("runtime_ready", _reject_runtime),
                        ("not_same_dev", _reject_same_dev), ("provider_label", _reject_provider_label),
                        ("role_enabled", _reject_role_mode), ("not_parked", _reject_parked),
                        ("blockers_done", _reject_blockers), ("senior_tier", _reject_tier),
                        ("hot_file_capacity", _reject_hot_file), ("quota_peer_route", _reject_quota_peer),
                        ("quota_capacity_route", _reject_capacity_route)):
        work_eligibility.register_candidate_rule(name, check)


def _reject_epic(_state, ticket: dict, _facts: dict) -> str | None:
    return "epics are lifecycle containers and cannot be offered as work" if verify_retry.is_epic(ticket) else None


def _reject_held(state: CandidateContext, ticket: dict, _facts: dict) -> str | None:
    return "ticket is held by an active lease" if ticket["key"] in state.held else None


def _reject_cooling(_state, ticket: dict, facts: dict) -> str | None:
    return "ticket is cooling down after a failed run" if ticket["key"] in facts.get("cooling", set()) else None


def _reject_runtime(state: CandidateContext, ticket: dict, facts: dict) -> str | None:
    step = str(facts.get("step") or "")
    return "the node is missing the container runtime required by this ticket" \
        if _needs_missing_runtime(state, ticket, step) else None


def _reject_same_dev(state: CandidateContext, ticket: dict, _facts: dict) -> str | None:
    dev = state.runs_repo.latest_development(ticket["key"])
    return "the development account cannot triage its own ticket" if dev and dev["account_id"] == state.account.id else None


def _reject_provider_label(state: CandidateContext, ticket: dict, _facts: dict) -> str | None:
    required = f"ai-{state.account.provider}"
    return f"ticket does not carry the required provider label {required}" \
        if state.account.explicit_only and required not in ticket["labels"] else None


def _reject_role_mode(state: CandidateContext, ticket: dict, facts: dict) -> str | None:
    role = _STEP_ROLE[str(facts["step"])]
    mode = state.modes.get(role)
    if mode == "on" or (mode == "probation" and role_health.probation_ticket(ticket)):
        return None
    return f"{role} role is {mode or 'off'} for this account"


def _reject_parked(_state, ticket: dict, facts: dict) -> str | None:
    return "ticket is parked pending a tool or environment fix" if ticket["key"] in facts["parked"] else None


def _reject_blockers(_state, ticket: dict, facts: dict) -> str | None:
    waiting = [key for key in ticket["blocked_by"] if key not in facts["done"]]
    return f"ticket is blocked by unfinished work: {', '.join(waiting)}" if waiting else None


def _reject_tier(state: CandidateContext, ticket: dict, _facts: dict) -> str | None:
    return "ticket requires a senior account" if "tier-senior" in ticket["labels"] \
        and state.account.tier != "senior" else None


def _reject_hot_file(state: CandidateContext, ticket: dict, facts: dict) -> str | None:
    if facts["flying"] is None:
        facts["flying"] = hotfiles.in_flight_hot_files(state.ctx.db, facts["footprint"])
    prediction = facts["footprint"](ticket)
    blocked = hotfiles.blocking_ticket(prediction, {key: value for key, value in facts["flying"].items()
                                                     if key != ticket["key"]},
                                       slots=state.ctx.settings.hot_file_slots)
    if blocked:
        hotfiles.mark_blocked(state.ctx.db, ticket["key"])
        return f"hot files are held by {blocked[0]}: {', '.join(blocked[1])}"
    hotfiles.clear_blocked(state.ctx.db, ticket["key"])
    return None


def _reject_quota_peer(_state, ticket: dict, facts: dict) -> str | None:
    return "an eligible same-provider peer can take this ticket" \
        if (ticket["key"], ticket["step"]) in facts["peer_offers"] else None


def _reject_capacity_route(_state, ticket: dict, facts: dict) -> str | None:
    key = (ticket["key"], ticket["step"])
    if ticket["step"] == "dev" and key not in facts["selected"]:
        return "a quota-safer eligible account was selected for this ticket"
    return None


def _eligible(state: CandidateContext, ticket: dict, step: str, facts: dict) -> bool:
    facts["step"] = step
    result = work_eligibility.evaluate_candidate(state, ticket, _RULES_BY_STEP[step], facts)
    return result.eligible


def _register_strategies() -> None:
    for name, role, phase, gather in (
        ("verification", "verify", 0, _verify), ("post_merge", "release", 0, _release),
        ("triage", "master", 0, _triage), ("intake", "intake_refiner", 0, _intake),
        ("impact", "intake_refiner", 0, _impact), ("development", "dev", 1, _development),
    ):
        register_strategy(name, role, phase, gather)


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
    running = RunsRepository(ctx.db).running_counts_by_project()
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
    ordinary = _collect_phase(state, 0)
    ordinary.sort(key=lambda ticket: (state.priorities.get(ticket["project"], 5), ticket["jira_created"] or _now()))
    development = _collect_phase(state, 1)
    return _route_candidates(state, _sort_candidates(ordinary, development))


def _collect_phase(state: CandidateContext, phase: int) -> list[dict]:
    return [ticket for strategy in _ROLE_STRATEGIES.values()
            if strategy.role in state.roles and strategy.phase == phase
            for ticket in strategy.collect(state)]


def _sort_candidates(ordinary: list[dict], development: list[dict]) -> list[dict]:
    offered = ordinary + development
    offered.sort(key=lambda ticket: "flow-bug" not in (ticket.get("labels") or []))
    return offered


def _route_candidates(state: CandidateContext, offered: list[dict]) -> list[dict]:
    if state.quota_at_risk:
        offered = _apply_peer_route(state, offered)
    if state.capacity_routing and offered:
        offered = _apply_capacity_route(state, offered)
    return offered


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
    cooling = state.runs_repo.cooling_tickets("post_merge")
    pending = [item for key in state.projects for item in environment_reports.pending_post_merge(state.ctx, key)]
    tickets = state.tickets_repo
    out = []
    for waiting in pending:
        ticket = tickets.by_key(waiting["ticket"])
        if not ticket or not _eligible(state, ticket, "post_merge", {"cooling": cooling}):
            continue
        out.append({**ticket, "step": "post_merge", "probation": _probation(state, "release")})
    return out


def _triage(state: CandidateContext) -> list[dict]:
    cooling = state.runs_repo.cooling_tickets("triage")
    tickets = state.tickets_repo.triage_candidates(state.projects)
    out = []
    for ticket in tickets:
        if not _eligible(state, ticket, "triage", {"cooling": cooling}):
            continue
        out.append({**ticket, "step": "triage", "probation": _probation(state, "master")})
    return out


def _intake(state: CandidateContext) -> list[dict]:
    waiting = state.runs_repo.intake_waiting_tickets("intake")
    tickets = state.tickets_repo.intake_candidates(state.projects)
    return _simple_intake(state, tickets, waiting, "intake")


def _impact(state: CandidateContext) -> list[dict]:
    waiting = state.runs_repo.intake_waiting_tickets("impact")
    tickets = state.tickets_repo.impact_candidates(state.projects)
    return _simple_intake(state, tickets, waiting, "impact")


def _simple_intake(state: CandidateContext, tickets: list[dict], waiting: set[str], step: str) -> list[dict]:
    out = []
    for ticket in tickets:
        if not _eligible(state, ticket, step, {"cooling": waiting}):
            continue
        out.append({**ticket, "step": step, "probation": _probation(state, "intake_refiner")})
    return out


def _development(state: CandidateContext) -> list[dict]:
    tickets_repo = state.tickets_repo
    done = tickets_repo.done_keys()
    cooling = state.runs_repo.cooling_tickets("dev")
    parked = tooling.parked_tickets(state.ctx, state.node_row) | environment_reports.parked_tickets(state.ctx)
    footprints: dict[str, dict] = {}

    def footprint(ticket: dict) -> dict:
        key = ticket["key"]
        if key not in footprints:
            footprints[key] = hotfiles.predict(state.ctx.db, ticket, tickets_repo.epic_key(ticket))
        return footprints[key]

    tickets = tickets_repo.development_candidates(state.projects)
    out = []
    facts = {"done": done, "cooling": cooling, "parked": parked, "footprint": footprint, "flying": None}
    for ticket in tickets:
        if not _eligible(state, ticket, "dev", facts):
            continue
        out.append({**ticket, "step": "dev", "probation": _probation(state, "dev")})
    out.sort(key=lambda ticket: (state.priorities.get(ticket["project"], 5),
                                 "rework" not in ticket["labels"],
                                 _priority(ticket.get("priority")), ticket["jira_created"] or _now()))
    return out


def _needs_missing_runtime(state: CandidateContext, ticket: dict, step: str) -> bool:
    if not state.missing_container_runtime:
        return False
    project = state.repo.projects.get(str(ticket.get("project") or ""))
    if not project:
        return False
    return _selected_settings_use_containers(state, ticket, project, step)


def _selected_settings_use_containers(state: CandidateContext, ticket: dict, project, step: str) -> bool:
    selected = project.settings_for_ticket({"key": ticket.get("key"), "type": ticket.get("type"),
                                            "epic": state.tickets_repo.epic_key(ticket)})
    if step in {"dev", "verify", "qa", "triage"}:
        prior = state.runs_repo.latest_development(str(ticket.get("key") or ""), delivered_only=True) \
            or state.runs_repo.latest_development(str(ticket.get("key") or ""))
        details = (prior or {}).get("details") or {}
        repository = str(details.get("repository") or "")
        if repository:
            saved = details.get("repository_settings")
            selected = (dict(saved) if isinstance(saved, dict) else project.settings_for_repo(repository))
    return bool(selected.get("containers"))


def _apply_peer_route(state: CandidateContext, offered: list[dict]) -> list[dict]:
    available: set[tuple[str, str]] = set()
    for peer in state.quota_peers(state.account.id, state.roles):
        for node in peer["nodes"]:
            if not state.peer_compatible(state.ctx, node, state.releases)["compatible"]:
                continue
            available.update((ticket["key"], ticket["step"])
                             for ticket in state.peer_offers(peer["id"], node, state.verify_retry_guard))
    return [ticket for ticket in offered if work_eligibility.evaluate_candidate(
        state, ticket, ("quota_peer_route",), {"peer_offers": available}).eligible]


def _apply_capacity_route(state: CandidateContext, offered: list[dict]) -> list[dict]:
    routed = state.capacity_route(state.account.id, state.node, offered, state.verify_retry_guard)
    selected = {(ticket["key"], ticket["step"]) for ticket in routed}
    return [ticket for ticket in offered if work_eligibility.evaluate_candidate(
        state, ticket, ("quota_capacity_route",), {"selected": selected}).eligible]


def _allowed(state: CandidateContext, role: str, ticket: dict) -> bool:
    mode = state.modes.get(role)
    return mode == "on" or (mode == "probation" and role_health.probation_ticket(ticket))


def _probation(state: CandidateContext, role: str) -> bool:
    return state.modes.get(role) == "probation"


def _priority(value: str | None) -> int:
    return {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}.get(value, 2)


def _now():
    return datetime.now(timezone.utc)


_register_rules()
_register_strategies()
