"""API surface for machine load limits.

Kept outside ``api.py`` so new policy routes do not add to the oversized control router module.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import load_policy
from . import licensing, rollout
from .db import J


class MachineLoadLimitsBody(BaseModel):
    model_config = {"extra": "forbid"}
    cpu_load_per_cpu: float = Field(gt=0, le=8)
    memory_used_pct: float = Field(gt=0, le=100)
    disk_free_pct: float = Field(ge=0, lt=100)
    host_cpu_load_per_cpu: float | None = Field(default=None, gt=0, le=8)


def register_machine_load_limits(app: FastAPI, ctx: Any, console_user: Callable, audit: Callable) -> None:
    """Register the owner-managed machine and shared-host threshold endpoint."""

    @app.put("/v1/machines/{name}/load-limits")
    def set_machine_load_limits(name: str, body: MachineLoadLimitsBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="machine-load", action="limits", node=name)
        row = ctx.db.one("SELECT metrics FROM nodes WHERE name=%s AND removed_at IS NULL", (name,))
        if not row:
            raise HTTPException(404, "machine is not registered")
        hostname = load_policy.host_name(row.get("metrics"))
        limits = {"cpu_load_per_cpu": body.cpu_load_per_cpu,
                  "memory_used_pct": body.memory_used_pct, "disk_free_pct": body.disk_free_pct}
        ctx.db.run("UPDATE nodes SET load_limits=%s,load_state='{}'::jsonb WHERE name=%s", (J(limits), name))
        if hostname and body.host_cpu_load_per_cpu is not None:
            ctx.db.run("INSERT INTO machine_host_load_limits (hostname,cpu_load_per_cpu,state,updated_at)"
                       " VALUES (%s,%s,'{}'::jsonb,now()) ON CONFLICT (hostname) DO UPDATE SET"
                       " cpu_load_per_cpu=EXCLUDED.cpu_load_per_cpu,state='{}'::jsonb,updated_at=now()",
                       (hostname, body.host_cpu_load_per_cpu))
        load_policy.record_heartbeat(ctx, name)
        audit("machine-load", "limits", name, "", None, user.subject, True, "load limits saved",
              {"machine": limits, "host": hostname,
               "host_cpu_load_per_cpu": body.host_cpu_load_per_cpu})
        host_limit = (body.host_cpu_load_per_cpu if body.host_cpu_load_per_cpu is not None else
                      load_policy.DEFAULT_HOST_LIMITS["cpu_load_per_cpu"])
        return {"machine": name, "hostname": hostname, "load_limits": limits,
                "host_load_limits": {"cpu_load_per_cpu": host_limit}}


def register_worker_candidate_routes(app: FastAPI, ctx: Any, node_auth: Callable, releases: Any,
                                     entitled_candidates: Callable, master_candidate: Callable) -> None:
    """Keep load-gated worker candidate handlers beside the scheduling policy."""

    @app.get("/v1/work/candidates")
    def candidates(account: str, node: str = Depends(node_auth), worker_id: str = Query("", max_length=128)) -> dict:
        if blocked := blocked_offer(ctx, node, "tickets"):
            return blocked
        try:
            state = licensing.enforce_work_entitlement(ctx, account, node,
                                                        public_key=ctx.settings.license_public_key)
            tickets, reason = entitled_candidates(account, node, worker_id, state)
        except licensing.LicenseError as exc:
            return {"tickets": [], "reason": str(exc)}
        policy = rollout.node_compatible(ctx, node, releases)
        if not policy["compatible"]:
            return {"tickets": [], "reason": f"version skew: {policy['reason']}"}
        result = {"tickets": [{"key": t["key"], "summary": t["summary"], "priority": t["priority"],
                               "labels": t["labels"], "step": t["step"]} for t in tickets[:20]]}
        if reason:
            result["reason"] = reason
        return result

    @app.get("/v1/master/candidate")
    def master_cycle_candidate(account: str, node: str = Depends(node_auth)) -> dict:
        if blocked := blocked_offer(ctx, node, "candidate"):
            return blocked
        try:
            licensing.enforce_work_entitlement(ctx, account, node, public_key=ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            return {"candidate": None, "reason": str(exc)}
        return {"candidate": master_candidate(account, node)}


def blocked_offer(ctx: Any, node: str, field: str) -> dict | None:
    capacity = load_policy.claim_limits(ctx, node)
    if capacity["allowed"]:
        return None
    return {field: None if field == "candidate" else [],
            "reason": capacity["reason"] or "machine load capacity is full"}


def work_claim_capacity(ctx: Any, node: str) -> tuple[dict, JSONResponse | None]:
    capacity = load_policy.claim_limits(ctx, node)
    response = (JSONResponse({"claimed": False, "reason": capacity["reason"] or "machine load capacity is full"},
                             status_code=409) if not capacity["allowed"] else None)
    return capacity, response


def version_claim_capacity(ctx: Any, node: str) -> dict:
    capacity = load_policy.claim_limits(ctx, node)
    if not capacity["allowed"]:
        raise HTTPException(409, capacity["reason"] or "machine load capacity is full")
    return capacity


def claim_run(ctx: Any, account: Any, values: tuple, capacity: dict, *, run_details: dict | None = None,
              quota_reservation_tokens: int | None = None) -> bool:
    """Use the same node and shared-host bounds when an atomic run claim is inserted."""
    from . import shared_accounts

    return shared_accounts.claim_run(ctx.db, account, values, max_node_runs=capacity["max_node_runs"],
                                     hostname=capacity["hostname"], max_host_runs=capacity["max_host_runs"],
                                     run_details=run_details, quota_reservation_tokens=quota_reservation_tokens)
