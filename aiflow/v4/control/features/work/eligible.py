"""Which tickets an account may take on a machine now (the rules of `GET /v1/work/candidates` and `POST /v1/work/claim`).

Eligibility is an ordered pipeline: early account, node and project rules prepare a shared scope, step collectors
preserve each query and filter sequence, and final routing applies the existing provider and capacity policies.
"""
from __future__ import annotations

from fastapi import HTTPException

from ... import (environment as environment_reports, flow_doctor, hotfiles, role_health, rollout, shared_accounts,
                 system_check, tooling, work_steps, worker_candidates)
from ... import dev_history
from ....core.services import service_gaps
from ....core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from .constants import BACKOFF_SQL, INTAKE_BACKOFF_SQL, PRIORITY, utcnow
from .eligibility_candidates import (dev_candidates, impact_candidates, intake_candidates,
                                     post_merge_candidates, triage_candidates, verify_candidates)
from .eligibility_scope import (Scope, account_may_work, node_accepts_work, online_node_row,
                                open_projects)

__all__ = [
    "BACKOFF_SQL", "CONTAINER_RUNTIME_TOOL", "HTTPException", "INTAKE_BACKOFF_SQL", "PRIORITY", "Scope",
    "account_may_work", "dev_candidates", "dev_history", "environment_reports", "flow_doctor", "hotfiles",
    "impact_candidates", "intake_candidates", "node_accepts_work", "online_node_row", "open_projects",
    "post_merge_candidates", "role_health", "rollout", "service_gaps", "shared_accounts", "system_check",
    "toolchain_gaps", "tooling", "triage_candidates", "utcnow", "verify_candidates", "work_steps",
    "worker_candidates", "eligible", "finalize",
]


def eligible(ctx, deps, account_id: str, node: str, *, routing: bool = True, capacity_routing: bool = True,
             ticket_key: str | None = None, lease_checked: bool = False,
             step_filter: str | None = None) -> list[dict]:
    """Tickets this account may take now; claims can scope revalidation to their requested ticket and step."""
    repo = ctx.repo()
    acc = repo.account(account_id)
    if not account_may_work(ctx, deps, acc, node, account_id):
        return []
    quota_at_risk = deps.quota_forecast_at_risk(account_id)
    node_row = online_node_row(ctx, node)
    if not node_row or not node_accepts_work(node_row):
        return []
    projects, missing_container_runtime = open_projects(
        ctx, repo, node, node_row, account_id, gap_checker=toolchain_gaps,
        service_gap_checker=service_gaps, runtime_tool=CONTAINER_RUNTIME_TOOL)
    roles = work_steps.roles_for_step(acc.roles or ["dev", "verify"], step_filter)
    if not roles:
        return []
    scope = Scope(ctx=ctx, deps=deps, repo=repo, account=acc, node=node, node_row=node_row,
                  projects=projects, roles=roles, ticket_key=ticket_key, account_id=account_id,
                  missing_container_runtime=missing_container_runtime, quota_at_risk=quota_at_risk,
                  backoff_sql=BACKOFF_SQL, intake_backoff_sql=INTAKE_BACKOFF_SQL, now=utcnow,
                  ticket_priority=PRIORITY, gap_checker=toolchain_gaps, runtime_tool=CONTAINER_RUNTIME_TOOL)
    # A role switched off for this account (docs/v4/role-health.md) takes one unresolved probation ticket.
    scope.role_modes = role_health.modes(ctx.db, acc.id, sorted(roles))
    # A claim already checked its ticket lease; the atomic acquisition below handles races.
    scope.held = set() if lease_checked else set(ctx.leases.held_keys())
    if not projects:
        return []
    scope.project_priority = {key: int(getattr(project, "priority", 5) or 5)
                              for key, project in repo.projects.items()}
    out = (verify_candidates(scope) + post_merge_candidates(scope) + triage_candidates(scope)
           + intake_candidates(scope) + impact_candidates(scope))
    out.sort(key=lambda ticket: (scope.project_priority.get(ticket["project"], 5),
                                 ticket["jira_created"] or scope.now()))
    out += dev_candidates(scope)
    return finalize(scope, out, routing=routing, capacity_routing=capacity_routing)


def finalize(scope: Scope, out: list[dict], *, routing: bool, capacity_routing: bool) -> list[dict]:
    """Apply the Flow doctor exclusion, workflow ordering, provider quota and capacity routing."""
    ctx, deps = scope.ctx, scope.deps
    out = flow_doctor.exclude_failed_routes(
        ctx, scope.account_id, scope.node, [ticket for ticket in out if ticket.get("type") != "Epic"])
    # Keep normal workflow order stable, but put a generated flow bug first whenever it is eligible.
    out.sort(key=lambda ticket: "flow-bug" not in (ticket.get("labels") or []))
    if routing and scope.ticket_key is None and out:
        out = ctx.provider_eligibility.filter_headroom(scope.account, out)
    if routing and scope.quota_at_risk and out:
        available_elsewhere: set[tuple[str, str]] = set()
        for peer in deps.quota_peer_accounts(scope.account_id, scope.roles):
            for peer_node in peer["nodes"]:
                if not rollout.node_compatible(ctx, peer_node, deps.releases())["compatible"]:
                    continue
                available_elsewhere.update(
                    (ticket["key"], ticket["step"])
                    for ticket in deps.eligible(peer["id"], peer_node, routing=False,
                                                capacity_routing=True, ticket_key=scope.ticket_key))
        out = [ticket for ticket in out if (ticket["key"], ticket["step"]) not in available_elsewhere]
    if capacity_routing and out:
        out = deps.route_capacity(scope.account_id, scope.node, out, ticket_key=scope.ticket_key)
    return out
