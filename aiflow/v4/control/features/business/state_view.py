"""What the console state endpoint returns to a given person: the shared snapshot, cut down to their projects and role."""
import copy
import threading
import time
from typing import Any

from ... import state as state_module


class StateSnapshotCache:
    """The lock and the last snapshots shared by the state requests of one application."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cache: dict[bool, tuple[float, dict[str, Any]]] = {}


def cached_snapshot(ctx, snapshots: StateSnapshotCache, include_signin: bool) -> dict[str, Any]:
    """Coalesce identical console snapshot reads while several views load at the same time."""
    requested_at = time.monotonic()
    with snapshots.lock:
        cached = snapshots.cache.get(include_signin)
        # A caller that began while the preceding snapshot was being built may share it.
        # Requests arriving after that read finished always see current database state.
        if cached and requested_at < cached[0]:
            return cached[1]
        data = state_module.snapshot(ctx, include_signin=include_signin)
        snapshots.cache[include_signin] = (time.monotonic(), data)
        return data


def trim_ticket_runs(data: dict[str, Any]) -> None:
    """Keep only the run fields a person without machine access may see on each ticket."""
    for ticket in data["tickets"]:
        if ticket.get("run"):
            ticket["run"] = {key: ticket["run"].get(key) for key in
                             ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress")}
        if ticket.get("last_run"):
            ticket["last_run"] = {key: ticket["last_run"].get(key) for key in
                                  ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                   "duration_s", "pr_url", "already_done", "ended_age_s")}


def totals_for_projects(ctx, visible: set[str]) -> dict[str, Any]:
    totals = ctx.db.one(
        "SELECT count(*) FILTER (WHERE started_at > now()-interval '24 hours') AS runs_24h,"
        " coalesce(sum(cost_usd) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS cost_24h,"
        " coalesce(sum(tokens_in) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_in_24h,"
        " coalesce(sum(tokens_out) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_out_24h,"
        " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome='done') AS done_24h,"
        " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome = ANY(%s)) AS failed_24h"
        " FROM runs WHERE project=ANY(%s)",
        (["failed", "blocked", "timeout", "lost"], list(visible)))
    return {key: int(value) if key in {"runs_24h", "tokens_in_24h", "tokens_out_24h",
                                       "done_24h", "failed_24h"} else float(value)
            for key, value in totals.items()}


def restrict_to_projects(ctx, data: dict[str, Any], visible: set[str]) -> dict[str, Any]:
    """The snapshot of a person who may see only some projects (a copy; the cached snapshot is shared)."""
    data = copy.deepcopy(data)
    all_tickets = data["tickets"]
    data["projects"] = [project for project in data["projects"] if project["key"] in visible]
    data["tickets"] = [ticket for ticket in all_tickets if ticket["project"] in visible]
    ticket_keys = {ticket["key"] for ticket in data["tickets"]}
    data["active_runs"] = [run for run in data["active_runs"] if run.get("project") in visible]
    data["recent_runs"] = [run for run in data["recent_runs"] if run.get("ticket") in ticket_keys]
    data["events"] = [event for event in data["events"] if event.get("ticket") in ticket_keys]
    # Project level artifacts carry different shapes across releases; omit aggregates for restricted users.
    data["release_readiness"] = []
    data["release_progress"] = []
    data["rollout"] = {}
    data["totals"] = totals_for_projects(ctx, visible)
    visible_nodes = {node["name"] for node in data["nodes"] if visible.intersection(node.get("projects") or [])}
    data["nodes"] = [node for node in data["nodes"] if node["name"] in visible_nodes]
    data["accounts"] = [account for account in data["accounts"]
                        if any(machine.get("node") in visible_nodes for machine in account.get("machines") or [])]
    data["incidents"] = [item for item in data["incidents"]
                         if item.get("subject") in visible or item.get("subject") in visible_nodes]
    data["incidents_recent"] = [item for item in data["incidents_recent"]
                                 if item.get("subject") in visible or item.get("subject") in visible_nodes]
    data["sa"] = {}
    data["version_policy"] = {}
    return data


def hide_machines_and_detail(data: dict[str, Any]) -> None:
    """The snapshot of a person without machine access: no nodes, accounts or incidents, and only summary run fields."""
    data["nodes"] = []
    data["accounts"] = []
    data["sa"] = {}
    data["role_health"] = {"roles": [], "settings": {}}
    data["version_policy"] = {}
    data["incidents"] = []
    data["incidents_recent"] = []
    data["events"] = [{key: event.get(key) for key in ("ts", "type", "ticket", "outcome", "reason")}
                       for event in data["events"]]
    trim_ticket_runs(data)
    data["active_runs"] = [{key: run[key] for key in ("id", "ticket", "project", "summary", "step",
                                                       "elapsed_s", "heartbeat_age_s", "lease_ms", "progress")}
                            for run in data["active_runs"]]
    data["recent_runs"] = [{key: run[key] for key in ("id", "ticket", "summary", "step", "outcome",
                                                       "error_kind", "duration_s", "pr_url", "already_done",
                                                       "ended_age_s")}
                            for run in data["recent_runs"]]
    trim_ticket_runs(data)
