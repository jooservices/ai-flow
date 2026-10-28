"""HTTP adapters and scheduling hooks for the system readiness check."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import system_check
from .github import GITHUB_APP_REQUIRED_PERMISSIONS

log = logging.getLogger("aiflow4.api")
_REFRESH_LOCK = threading.Lock()
_PENDING_REFRESHES: set[str] = set()


def validate_github_app_permissions(details: dict[str, Any]) -> None:
    permissions = details.get("permissions") or {}
    missing = [name for name, level in GITHUB_APP_REQUIRED_PERMISSIONS.items()
               if permissions.get(name) != level]
    if missing:
        raise ValueError("GitHub App does not have the permissions AI Flow requires")


class HeartbeatBody(BaseModel):
    """Heartbeat fields from node agents, including the sanitized readiness report."""

    slots_total: int = 1
    slots_used: int = 0
    release_version: str = ""
    release_protocol: int = 0
    tools: dict[str, Any] = Field(default_factory=dict)
    services: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    workers: list[dict[str, Any]] = Field(default_factory=list)
    credential_expiries: dict[str, datetime | None] = Field(default_factory=dict)
    system_conditions: list[dict[str, Any]] = Field(default_factory=list)
    credential_checks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    auto_fixed_tools: list[str] = Field(default_factory=list)
    auto_fixed_conditions: list[str] = Field(default_factory=list)
    runtime_settings: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    runtime_settings_public_key: str = Field(default="", max_length=2000)
    environment_items: dict[str, list[str]] = Field(default_factory=dict)


def _refresh_join(ctx, node: str) -> None:
    try:
        system_check.refresh_node(ctx, node, "machine_joined")
    except Exception as exc:  # noqa: BLE001 - a readiness outage must not affect a completed join
        log.warning("machine-join system check failed for node %s (%s)", node, type(exc).__name__)


def schedule_join_readiness(ctx, node: str) -> None:
    """Run provider probes after returning the one-use machine join response."""
    threading.Thread(target=_refresh_join, args=(ctx, node),
                     name=f"aiflow4-join-readiness-{node}", daemon=True).start()


def _refresh_heartbeat(ctx, node: str, trigger: str) -> None:
    try:
        system_check.refresh_node(ctx, node, trigger)
    except Exception as exc:  # noqa: BLE001 - a check outage must not delay desired state
        log.warning("heartbeat system check failed for node %s (%s)", node, type(exc).__name__)
    finally:
        with _REFRESH_LOCK:
            _PENDING_REFRESHES.discard(node)


def schedule_heartbeat_readiness(ctx, node: str, trigger: str) -> None:
    """Coalesce provider-backed checks so they do not delay heartbeat responses."""
    with _REFRESH_LOCK:
        if node in _PENDING_REFRESHES:
            return
        _PENDING_REFRESHES.add(node)
    threading.Thread(target=_refresh_heartbeat, args=(ctx, node, trigger),
                     name=f"aiflow4-heartbeat-readiness-{node}", daemon=True).start()


def refresh_heartbeat(ctx, node: str, body, previous: dict | None, reported_release: str) -> None:
    """Persist the heartbeat's scrubbed readiness data, then schedule external permission probes."""
    previous_release = (previous or {}).get("release_version") or ""
    trigger = "upgrade" if previous_release and previous_release != reported_release else "heartbeat"
    system_check.refresh_node(
        ctx, node, trigger,
        body.system_conditions if "system_conditions" in body.model_fields_set else None,
        body.auto_fixed_tools if "auto_fixed_tools" in body.model_fields_set else None,
        body.credential_checks if "credential_checks" in body.model_fields_set else None,
        body.auto_fixed_conditions if "auto_fixed_conditions" in body.model_fields_set else None,
        provider_checks=False)
    schedule_heartbeat_readiness(ctx, node, trigger)


def projects_readiness(ctx, repo) -> dict[str, list[dict[str, object]]]:
    """Return machine readiness for every project with one node query for the Projects screen."""
    nodes = ctx.db.all("SELECT name,readiness,projects,dynamic FROM nodes ORDER BY name")
    project_nodes = {key: [] for key in repo.projects}
    for node in nodes:
        name = node["name"]
        accounts = [account.id for account in repo.accounts_on(name)]
        for key in repo.projects_for_node(name, node.get("projects") or [], node.get("dynamic", False)):
            if key in project_nodes:
                project_nodes[key].append({"name": name, **system_check.project_node_readiness(
                    node.get("readiness"), key, accounts)})
    return project_nodes


def project_readiness(ctx, project, repo) -> list[dict[str, object]]:
    """Return safe machine readiness summaries for one project row in the Projects screen."""
    return projects_readiness(ctx, repo).get(project.key, [])


def block_reason(readiness: dict | None, project: str, account: str,
                 assigned_accounts: list[str]) -> str:
    reasons = system_check.blocking_reasons(readiness, project, account, assigned_accounts)
    if not reasons:
        return ""
    first = reasons[0]
    name = str(first.get("name") or "System check")
    guidance = str(first.get("action") or first.get("detail") or "Review this condition on the System check screen.")
    return f"System check: {name}. {guidance}"


def release_candidates(readiness: dict | None, account: str, assigned_accounts: list[str],
                       candidates: list[dict]) -> tuple[list[dict], str]:
    """Keep project release work that passes readiness; return the reason when all candidates are blocked."""
    allowed, blocked = [], []
    for candidate in candidates:
        reason = block_reason(readiness, candidate["project"], account, assigned_accounts)
        if reason:
            blocked.append(reason)
        else:
            allowed.append(candidate)
    return allowed, (blocked[0] if candidates and not allowed else "")


def release_candidate_refusal(readiness: dict | None, account: str, assigned_accounts: list[str],
                              candidates: list[dict]) -> dict | None:
    """Return the existing API refusal shape if readiness blocks every release candidate."""
    allowed, reason = release_candidates(readiness, account, assigned_accounts, candidates)
    return {"versions": [], "reason": reason} if reason else None


def require_project_ready(readiness: dict | None, project: str, account: str,
                          assigned_accounts: list[str]) -> None:
    reason = block_reason(readiness, project, account, assigned_accounts)
    if reason:
        raise HTTPException(409, reason)


def register_routes(app: FastAPI, ctx, console_user, optional_console_user) -> None:
    """Mount authenticated System check and support-bundle endpoints on the control API."""
    @app.get("/v1/system-check")
    def system_check_report(request: Request) -> dict:
        user = (console_user(request, "node.operations.read") if ctx.settings.console_login
                else optional_console_user(request))
        return {"nodes": system_check.report_nodes(ctx, user.project_keys if user else None)}

    @app.post("/v1/system-check/run")
    def system_check_run(request: Request) -> dict:
        console_user(request, "nodes.manage", target="system-check", action="run")
        return {"nodes": system_check.refresh_all(ctx, "manual")}

    @app.get("/v1/support-bundle")
    def support_bundle(request: Request) -> JSONResponse:
        user = (console_user(request, "node.operations.read") if ctx.settings.console_login
                else optional_console_user(request))
        return JSONResponse(system_check.support_bundle(ctx, user.project_keys if user else None), headers={
            "Content-Disposition": 'attachment; filename="aiflow4-support.json"',
            "Cache-Control": "no-store",
        })
