"""Which tickets an account may take on a machine now (the rules of `GET /v1/work/candidates` and `POST /v1/work/claim`).

Moved verbatim out of the control app (JAIF-481). It needs a few helpers that live in the app (`deps`: the pause and
quota checks, the previous dev run, the epic of a ticket, capacity routing, and the releases directory); the function body
is otherwise unchanged."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from ... import (environment as environment_reports, flow_doctor, hotfiles, role_health, rollout, shared_accounts,
                 system_check, tooling, work_steps, worker_candidates)
from ... import dev_history
from ....core.services import service_gaps
from ....core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from .constants import BACKOFF_SQL, INTAKE_BACKOFF_SQL, PRIORITY, utcnow


def eligible(ctx, deps, account_id: str, node: str, *, routing: bool = True, capacity_routing: bool = True,
             ticket_key: str | None = None, lease_checked: bool = False,
             step_filter: str | None = None) -> list[dict]:
    """Tickets this account may take now; claims can scope revalidation to their requested ticket and step."""
    repo = ctx.repo()
    acc = repo.account(account_id)
    if not acc or not acc.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    if acc.paused or deps.account_master_paused(account_id):
        return []
    if ctx.leases.cooling(account_id):
        return []
    if not shared_accounts.may_take(ctx.db, acc, node)[0]:  # total session limit and this machine's share
        return []
    quota_at_risk = deps.quota_forecast_at_risk(account_id)
    node_row = ctx.db.one("SELECT projects,dynamic,tools,metrics,readiness,accepting_work FROM nodes WHERE name=%s"
                          " AND removed_at IS NULL AND status='online' AND last_heartbeat IS NOT NULL"
                          " AND last_heartbeat > now() - make_interval(secs => %s)",
                          (node, shared_accounts.NODE_ONLINE_S))
    # Never route from persisted pre-reboot readiness; the agent must report a fresh heartbeat first.
    if not node_row:
        return []
    if not node_row.get("accepting_work", True):
        return []
    projects = repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False))
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
    missing_container_runtime = bool(toolchain_gaps(node_tools, [CONTAINER_RUNTIME_TOOL], repo.versions))

    def ticket_needs_missing_container_runtime(ticket: dict[str, Any], step: str) -> bool:
        project = repo.projects.get(str(ticket.get("project") or ""))
        if not project or not missing_container_runtime:
            return False
        selected = project.settings_for_ticket({"key": ticket.get("key"), "type": ticket.get("type"),
                                                 "epic": deps.ticket_epic(ticket)})
        # Verify and rework restore the repository settings captured by the development run. Gate on those
        # same settings so a later project remap cannot offer container work to a node without the runtime.
        if step in {"dev", "verify", "qa", "triage"}:
            prior_dev = dev_history.delivered_or_last(ctx.db, str(ticket.get("key") or ""))
            details = (prior_dev or {}).get("details") or {}
            prior_repository = str(details.get("repository") or "")
            if prior_repository:
                prior_settings = details.get("repository_settings")
                selected = (dict(prior_settings) if isinstance(prior_settings, dict)
                            else project.settings_for_repo(prior_repository))
        return bool(selected.get("containers"))

    # A project with a declared stack is offered only after this node reports every pinned tool healthy.
    # Filtering here keeps another project on the same node available while a stack install is pending.
    projects = [p for p in projects if not (repo.projects[p].stacks or repo.projects[p].uses_containers)
                or not toolchain_gaps(node_tools,
                                      [tool for tool in repo.projects[p].tools if tool != CONTAINER_RUNTIME_TOOL],
                                      repo.versions, repo.projects[p].stacks,
                                      require_pins=bool(repo.projects[p].stacks))]
    # Declared test services (PostgreSQL, Redis) must be reported healthy too; other projects stay available.
    reported_services = (node_row.get("metrics") or {}).get("services")
    projects = [p for p in projects
                if not service_gaps(reported_services, repo.projects[p].required_services)]
    projects = [p for p in projects if not system_check.blocking_reasons(node_row.get("readiness"), p, account_id)]
    roles = work_steps.roles_for_step(acc.roles or ["dev", "verify"], step_filter)
    if not roles:
        return []
    scoped_key = " AND key=%s" if ticket_key is not None else ""
    scoped_tickets_key = " AND tickets.key=%s" if ticket_key is not None else ""
    scoped_ticket = (ticket_key,) if ticket_key is not None else ()

    def backoff_rows(step: str) -> list[dict]:
        query = BACKOFF_SQL
        params = {"step": step}
        if ticket_key is not None:
            marker = "AND outcome IN ('failed', 'timeout', 'lost', 'no_changes')"
            query = query.replace(marker, marker + "\n    AND ticket_key = %(ticket_key)s", 1)
            params["ticket_key"] = ticket_key
        return ctx.db.all(query, params)

    def intake_backoff_rows(step: str) -> list[dict]:
        query = INTAKE_BACKOFF_SQL
        params = {"step": step}
        if ticket_key is not None:
            marker = "ORDER BY ticket_key, started_at DESC"
            query = query.replace(marker, "AND ticket_key = %(ticket_key)s\n" + marker, 1)
            params["ticket_key"] = ticket_key
        return ctx.db.all(query, params)
    # a role switched off for this account (docs/v4/role-health.md) takes one unresolved probation ticket
    modes = role_health.modes(ctx.db, acc.id, sorted(roles))

    def allowed(role: str, t: dict) -> bool:
        return modes.get(role) == "on" or (modes.get(role) == "probation" and role_health.probation_ticket(t))

    def probation(role: str) -> bool:
        return modes.get(role) == "probation"

    # A claim has already checked this ticket's lease above and the atomic lease acquisition below handles
    # races. Avoid scanning every Redis lease when revalidating that one requested ticket.
    held = set() if lease_checked else set(ctx.leases.held_keys())
    out: list[dict] = []
    if not projects:
        return []
    prio = {k: int(getattr(p, "priority", 5) or 5) for k, p in repo.projects.items()}
    if "verify" in roles:
        verify_cooling = {r["ticket_key"] for r in backoff_rows("verify")}
        for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
                            " AND NOT ('aiflow-hold' = ANY(labels))" + scoped_key,
                            (projects, *scoped_ticket)):
            if ticket_needs_missing_container_runtime(t, "verify"):
                continue
            if t["key"] in held or t["key"] in verify_cooling:
                continue
            dev = deps.last_dev(t["key"], done=True)
            if not dev or not dev["pr_url"]:
                continue
            if dev["account_id"] == acc.id:  # never grade your own work
                continue
            if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                continue
            if not allowed("verify", t):
                continue
            out.append({**t, "step": "verify", "probation": probation("verify")})
    if "release" in roles:
        post_merge_cooling = {r["ticket_key"] for r in backoff_rows("post_merge")}
        post_merge_projects = projects
        if ticket_key is not None:
            ticket_project = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket_key,))
            project_key = str((ticket_project or {}).get("project") or "")
            post_merge_projects = [project_key] if project_key in projects else []
        pending = [item for project_key in post_merge_projects
                   for item in environment_reports.pending_post_merge(
                       ctx, project_key, [ticket_key] if ticket_key is not None else None)]
        for waiting in pending:
            t = ctx.db.one("SELECT * FROM tickets WHERE key=%s", (waiting["ticket"],))
            if not t or t["key"] in held or t["key"] in post_merge_cooling:
                continue
            if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                continue
            if not allowed("release", t):
                continue
            out.append({**t, "step": "post_merge", "probation": probation("release")})
    if "master" in roles:
        triage_cooling = {r["ticket_key"] for r in backoff_rows("triage")}
        # Triage decides a raised escalation; a ticket in Needs owner without an open one has nothing to decide
        for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'"
                            " AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))"
                            " AND EXISTS (SELECT 1 FROM escalations e WHERE e.ticket_key = tickets.key"
                            " AND e.resolution = 'open')" + scoped_tickets_key,
                            (projects, *scoped_ticket)):
            if ticket_needs_missing_container_runtime(t, "triage"):
                continue
            if t["key"] in held or t["key"] in triage_cooling:
                continue
            dev = deps.last_dev(t["key"])
            if dev and dev["account_id"] == acc.id:
                continue
            if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                continue
            if not allowed("master", t):
                continue
            out.append({**t, "step": "triage", "probation": probation("master")})
    if "intake_refiner" in roles:
        def backoff(step: str) -> set[str]:
            return {r["ticket_key"] for r in intake_backoff_rows(step)
                    if r["outcome"] in ("failed", "timeout", "lost") and r["ended_at"]
                    and r["ended_at"] > utcnow() - timedelta(minutes=10)}
        waiting = backoff("intake")
        for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'"
                            " AND NOT ('aiflow-hold' = ANY(labels))" + scoped_key
                            + " ORDER BY jira_created NULLS LAST, key", (projects, *scoped_ticket)):
            if ticket_needs_missing_container_runtime(t, "intake"):
                continue
            if t["key"] in held or t["key"] in waiting:
                continue
            if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                continue
            if not allowed("intake_refiner", t):
                continue
            out.append({**t, "step": "intake", "probation": probation("intake_refiner")})
        # change requests from the Business view: assess the impact once, while they wait in Backlog
        waiting = backoff("impact")
        for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'"
                            " AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
                            " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key"
                            " AND r.step = 'impact' AND r.outcome = 'assessed')" + scoped_tickets_key
                            + " ORDER BY jira_created NULLS LAST, key", (projects, *scoped_ticket)):
            if ticket_needs_missing_container_runtime(t, "impact"):
                continue
            if t["key"] in held or t["key"] in waiting:
                continue
            if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                continue
            if not allowed("intake_refiner", t):
                continue
            out.append({**t, "step": "impact", "probation": probation("intake_refiner")})
    out.sort(key=lambda t: (prio.get(t["project"], 5), t["jira_created"] or utcnow()))
    if "dev" in roles:
        cooling = {r["ticket_key"] for r in backoff_rows("dev")}
        dev_rows = []
        scope = [ticket_key] if ticket_key is not None else None
        parked = tooling.parked_tickets(ctx, node_row, scope)  # waiting for a tool this machine lacks
        parked |= environment_reports.parked_tickets(ctx, scope)  # waiting for an environment item
        footprints: dict[str, dict] = {}
        footprint_history: dict[str, bool] = {}
        epics: dict[str, str] = {}
        dev_tickets = ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s)"
                                 + worker_candidates.DEV_TICKET_SQL + scoped_key,
                                 (projects, *scoped_ticket))
        blocker_keys = sorted({str(blocker) for ticket in dev_tickets
                               for blocker in ticket.get("blocked_by") or []})
        done = ({row["key"] for row in ctx.db.all(
            "SELECT key FROM tickets WHERE key=ANY(%s) AND status='Done'", (blocker_keys,))}
                if blocker_keys else set())
        # Fresh tickets without a parent have no historical hot-file footprint unless they already have a
        # successful dev run that recorded changed files. Batch that lookup instead of querying history once
        # per candidate on every worker poll.
        dev_keys = [t["key"] for t in dev_tickets]
        history_keys = ({row["ticket_key"] for row in ctx.db.all(
            "SELECT DISTINCT ticket_key FROM runs WHERE ticket_key = ANY(%s) AND step='dev' AND outcome='done'"
            " AND details ? 'changed_files'", (dev_keys,))} if dev_keys else set())
        blocked_keys = ({row["ticket_key"] for row in ctx.db.all(
            "SELECT ticket_key FROM hot_file_blocked WHERE ticket_key = ANY(%s)", (dev_keys,))}
            if dev_keys else set())

        def footprint(row: dict, include_history: bool = False) -> dict:
            key = str(row["key"])
            if key not in epics:
                epics[key] = deps.ticket_epic(row)
            include = include_history or bool(epics[key]) or key in history_keys
            if key not in footprints or (include and not footprint_history.get(key, False)):
                footprints[key] = hotfiles.predict(ctx.db, row, epics[key], include_history=include)
                footprint_history[key] = include
            return footprints[key]

        flying: dict[str, list[str]] | None = None
        for t in dev_tickets:
            if ticket_needs_missing_container_runtime(t, "dev"):
                continue
            if not worker_candidates.may_offer_dev(t, done, held=held, cooling=cooling, parked=parked):
                continue
            labels = set(t["labels"])
            if "tier-senior" in labels and acc.tier != "senior":
                continue
            if acc.explicit_only and f"ai-{acc.provider}" not in labels:
                continue
            if not allowed("dev", t):
                continue
            candidate_footprint = footprint(t)
            if not candidate_footprint["hot"]:
                if t["key"] in blocked_keys:
                    hotfiles.clear_blocked(ctx.db, t["key"])
                    blocked_keys.discard(t["key"])
                dev_rows.append({**t, "step": "dev", "probation": probation("dev")})
                continue
            # never start two tickets expected to touch the same hot file (docs/v4/merge-conflicts.md)
            if flying is None:
                flying = hotfiles.in_flight_hot_files(
                    ctx.db, lambda row: footprint(row, include_history=True))
            if hotfiles.blocking_ticket(candidate_footprint,
                                       {k: v for k, v in flying.items() if k != t["key"]},
                                       slots=ctx.settings.hot_file_slots):
                # A hot-file holder keeps its reservation through development and review; waiting never permits overlap.
                if t["key"] not in blocked_keys:
                    hotfiles.mark_blocked(ctx.db, t["key"])
                    blocked_keys.add(t["key"])
                continue
            elif t["key"] in blocked_keys:
                hotfiles.clear_blocked(ctx.db, t["key"])
                blocked_keys.discard(t["key"])
            dev_rows.append({**t, "step": "dev", "probation": probation("dev")})
        dev_rows.sort(key=lambda t: (prio.get(t["project"], 5), "rework" not in t["labels"],
                                     PRIORITY.get(t["priority"], 2), t["jira_created"] or utcnow()))
        out += dev_rows
    # Routes the Flow doctor moved a ticket away from (failed account or machine) are not offered again.
    out = flow_doctor.exclude_failed_routes(ctx, account_id, node, [t for t in out if t.get("type") != "Epic"])
    # Keep the normal workflow order stable, but put a generated flow bug first whenever it is eligible.
    out.sort(key=lambda t: "flow-bug" not in (t.get("labels") or []))
    if routing and ticket_key is None and out:  # spend the provider with the most quota left first (docs/v4/provider-routing.md)
        out = ctx.provider_eligibility.filter_headroom(acc, out)
    if routing and quota_at_risk and out:
        # Prefer peers only for tickets they can actually claim. A peer on another node, with different
        # project access or roles, must not leave this account's otherwise-routable tickets waiting.
        available_elsewhere: set[tuple[str, str]] = set()
        for peer in deps.quota_peer_accounts(account_id, roles):
            for peer_node in peer["nodes"]:
                if not rollout.node_compatible(ctx, peer_node, deps.releases())["compatible"]:
                    continue
                available_elsewhere.update((ticket["key"], ticket["step"])
                                           for ticket in deps.eligible(peer["id"], peer_node, routing=False,
                                                                  capacity_routing=True,
                                                                  ticket_key=ticket_key))
        out = [ticket for ticket in out if (ticket["key"], ticket["step"]) not in available_elsewhere]
    if capacity_routing and out:
        out = deps.route_capacity(account_id, node, out, ticket_key=ticket_key)
    return out
