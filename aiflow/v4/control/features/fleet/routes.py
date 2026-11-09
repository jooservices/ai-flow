"""Routes of the fleet feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from ...system_check_api import HeartbeatBody
from . import heartbeat
from .bodies import CheckProbeBody, CheckProbeTokenBody, ConfirmBody, JoinBody, JoinCommandBody, JoinInspectBody, JoinTokenBody, ManagedAccountCreateBody, ManagedAccountSignInStatusBody, ManagedAccountUpdateBody, NodeOperationBody, NodeOperationCheckpointBody, NodeOperationResultBody
from .events import (AiAccountAdded, AiAccountManagementChanged, AiAccountRemoved, AiAccountSignInCompleted,
                     AiAccountSignInRestarted, CheckProbeTokenIssued, NodeJoinCommandCreated, NodeJoined,
                     NodeOperationQueued, NodeRemoved, NodeWorkAcceptanceChanged)
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException, Request
from ....core.controlrepo import Account, ControlRepo
from ....core.services import service_gaps
from ....core.toolchains import (CONTAINER_RUNTIME_TOOL, missing_tools, pinned_version, toolchain_gaps)
from ... import (flow_doctor_api, licensing, load_policy, load_policy_api,
               log_viewer, quota_refresh, runtime_settings, system_check_api, tooling)
from ... import environment as environment_reports
from ...db import J
from ...platform.events.wiring import dispatcher_for, publish_for as _publish_for
from ...route_deps import github_connection_used, h
from ...features.work.constants import utcnow
from .routes_accounts import install_managed_account_routes
from .routes_join import install_join_command_routes
from .routes_node_admin import install_node_admin_routes
from .routes_node_lifecycle import install_node_lifecycle_routes
from .routes_operation_admin import install_node_operation_admin_routes
from .routes_operations import install_node_operation_agent_routes
from .routes_probes import install_check_probe_routes
from .routes_system_check import install_system_check_routes

@dataclass(frozen=True, slots=True)
class FleetRouteHelpers:
    """Per-install helpers shared by fleet route groups and returned to existing callers."""

    ctx: object
    JOB_ROLES: dict[str, str]

    def account_jobs(self, roles: list[str]) -> list[str]:
        return [job for job, role in self.JOB_ROLES.items() if role in roles]

    def lock_machine_capacity(self, connection, name: str) -> None:
        """Serialize all account session-budget checks for this machine across API processes."""
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"aiflow4-machine-sessions:{name}",))

    def machine_capacity(self, repo: ControlRepo, name: str) -> int:
        return sum(max(1, int(account.parallel or 1)) for account in repo.accounts_on(name))

    def save_managed_account(self, account: Account, roles: list[str], sessions: int, paused: bool,
                             sign_in_url: str = "", sign_in_code: str = "",
                             expires_at: datetime | None = None, connection=None) -> None:
        ctx = self.ctx
        sql = (
            "INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,parallel,explicit_only,paused,"
            "sign_in_url,sign_in_code,sign_in_expires_at,sign_in_started_at,removed_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s<>'' THEN now() ELSE NULL END,NULL)"
            " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider,node=EXCLUDED.node,tier=EXCLUDED.tier,"
            "roles=EXCLUDED.roles,parallel=EXCLUDED.parallel,explicit_only=EXCLUDED.explicit_only,paused=EXCLUDED.paused,"
            "sign_in_url=CASE WHEN EXCLUDED.sign_in_url='' THEN managed_ai_accounts.sign_in_url"
            " ELSE EXCLUDED.sign_in_url END,sign_in_code=CASE WHEN EXCLUDED.sign_in_code=''"
            " THEN managed_ai_accounts.sign_in_code ELSE EXCLUDED.sign_in_code END,"
            "sign_in_expires_at=coalesce(EXCLUDED.sign_in_expires_at,managed_ai_accounts.sign_in_expires_at),"
            "sign_in_started_at=CASE WHEN EXCLUDED.sign_in_code<>'' THEN now()"
            " ELSE managed_ai_accounts.sign_in_started_at END,"
            "removed_at=NULL,updated_at=now()")
        params = (account.id, account.provider, account.node, account.tier, roles, sessions, account.explicit_only,
                  paused, sign_in_url, sign_in_code, expires_at, sign_in_code)
        if connection is None:
            ctx.db.run(sql, params)
        else:
            connection.execute(sql, params)

    def operation_plan(self, name: str, body: NodeOperationBody, node_row: dict) -> dict:
        repo = self.ctx.repo()
        configured = repo.nodes.get(name)
        packages = (configured.packages or []) if configured else []
        services = (configured.services or []) if configured else []
        _validate_node_packages(packages)
        _validate_node_services(services)
        required = repo.tools_for_node(name)
        stacks = _node_operation_stacks(repo, configured)
        missing = set(toolchain_gaps(node_row.get("tools") or {}, required, repo.versions, stacks,
                                     require_pins=bool(stacks)))
        selected = _operation_components(body, required, missing, packages)
        _include_node_prerequisite(selected)
        tools = _operation_plan_tools(selected, packages, repo.versions, stacks)
        desired_services = list(services) if body.action == "repair" else []
        return {"action": body.action, "tools": tools, "services": desired_services}

    def probe_project(self, name: str, key: str):
        """The project when this machine serves it (a probe of anything else is refused)."""
        ctx = self.ctx
        repo = ctx.repo()
        row = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (name,)) or {}
        if key not in repo.projects_for_node(name, row.get("projects") or [], row.get("dynamic", False)):
            raise HTTPException(403, "the node does not serve this project")
        return repo.projects[key]


def _validate_node_packages(packages: list[str]) -> None:
    if not isinstance(packages, list) or any(
            not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,127}", package)
            for package in packages):
        raise HTTPException(400, "node packages must be a list of safe package names")


def _validate_node_services(services: list[dict]) -> None:
    if not isinstance(services, list) or any(not isinstance(service, dict) for service in services):
        raise HTTPException(400, "node services must be a list of service definitions")
    for service in services:
        _validate_node_service(service)


def _validate_node_service(service: dict) -> None:
    unit = service.get("unit")
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,180}\.service", unit):
        raise HTTPException(400, "node service unit is invalid")
    healthcheck = service.get("healthcheck", "")
    if not isinstance(healthcheck, str):
        raise HTTPException(400, "service health check must be an HTTP URL")
    if healthcheck:
        parsed = urlsplit(healthcheck)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            raise HTTPException(400, "service health check must be an HTTP URL without embedded credentials")


def _node_operation_stacks(repo, configured) -> list[str]:
    projects = configured.projects if configured else []
    return [stack for project in projects if project in repo.projects for stack in repo.projects[project].stacks]


def _include_node_prerequisite(selected: list[str]) -> None:
    ai_tools = {"claude", "codex", "grok", "opencode"}
    if any(tool in ai_tools for tool in selected) and "node" not in selected:
        selected.append("node")


def _operation_plan_tools(selected: list[str], packages: list[str], versions: dict, stacks: list[str]) -> list[dict]:
    return [{"name": tool, "version": pinned_version(versions, tool, stacks),
             "packages": list(packages) if tool == "packages" else []} for tool in selected]


def _repair_components(required: list[str], packages: list[str], component: str) -> list[str]:
    if component:
        raise HTTPException(400, "repair reconciles the full desired state; omit component")
    selected = [tool for tool in required if tool != CONTAINER_RUNTIME_TOOL]
    if packages:
        selected.append("packages")
    return selected


def _requested_component(component: str, required: list[str], missing: set[str], packages: list[str]) -> list[str]:
    if component == "packages":
        if not packages:
            raise HTTPException(400, "no OS packages are declared for this node")
        return [component]
    if component not in required:
        raise HTTPException(400, "component is not required by this node's control repo state")
    if component == CONTAINER_RUNTIME_TOOL:
        raise HTTPException(409, "configure rootless Podman and project quotas on the node")
    if component not in missing:
        raise HTTPException(409, "component is already present on this node")
    return [component]


def _install_components(component: str, required: list[str], missing: set[str], packages: list[str]) -> list[str]:
    if component:
        return _requested_component(component, required, missing, packages)
    selected = sorted(tool for tool in missing if tool != CONTAINER_RUNTIME_TOOL)
    if not selected and CONTAINER_RUNTIME_TOOL in missing:
        raise HTTPException(409, "configure rootless Podman and project quotas on the node")
    if not selected:
        raise HTTPException(409, "node has no missing required components")
    return selected


def _operation_components(body, required: list[str], missing: set[str], packages: list[str]) -> list[str]:
    if body.action == "repair":
        return _repair_components(required, packages, body.component)
    return _install_components(body.component.strip(), required, missing, packages)


__all__ = ['re', 'secrets', 'uuid', 'datetime', 'timedelta', 'SimpleNamespace', 'HeartbeatBody', 'heartbeat', 'CheckProbeBody', 'CheckProbeTokenBody', 'ConfirmBody', 'JoinBody', 'JoinCommandBody', 'JoinInspectBody', 'JoinTokenBody', 'ManagedAccountCreateBody', 'ManagedAccountSignInStatusBody', 'ManagedAccountUpdateBody', 'NodeOperationBody', 'NodeOperationCheckpointBody', 'NodeOperationResultBody', 'AiAccountAdded', 'AiAccountManagementChanged', 'AiAccountRemoved', 'AiAccountSignInCompleted', 'AiAccountSignInRestarted', 'CheckProbeTokenIssued', 'NodeJoinCommandCreated', 'NodeJoined', 'NodeOperationQueued', 'NodeRemoved', 'NodeWorkAcceptanceChanged', 'urlsplit', 'Depends', 'HTTPException', 'Request', 'Account', 'ControlRepo', 'service_gaps', 'CONTAINER_RUNTIME_TOOL', 'missing_tools', 'pinned_version', 'toolchain_gaps', 'flow_doctor_api', 'licensing', 'load_policy', 'load_policy_api', 'log_viewer', 'quota_refresh', 'runtime_settings', 'system_check_api', 'tooling', 'environment_reports', 'J', 'dispatcher_for', 'publish_for', 'github_connection_used', 'h', 'utcnow', 'install', 'audit_refused_write']

def publish_for(ctx, item, *, deliver_now=False, **kwargs):
    return _publish_for(ctx, item, deliver_now=deliver_now, **kwargs)


def audit_refused_write(audit, target, action, ticket, actor, result):
    audit(target, action, ticket, "", None, actor, False, result)


def install(app, ctx, deps) -> SimpleNamespace:
    helpers = FleetRouteHelpers(ctx, {"develop": "dev", "verify": "verify", "intake": "intake_refiner",
                                      "master": "master"})
    node_routes = install_node_lifecycle_routes(app, ctx, deps)
    join_routes = install_join_command_routes(app, ctx, deps)
    account_routes = install_managed_account_routes(app, ctx, deps, helpers)
    admin_routes = install_node_admin_routes(app, ctx, deps)
    operation_routes = install_node_operation_admin_routes(app, ctx, deps, helpers)
    probe_routes = install_check_probe_routes(app, ctx, deps, helpers)
    agent_routes = install_node_operation_agent_routes(app, ctx, deps)
    install_system_check_routes(app, ctx, deps)
    return SimpleNamespace(
        issue_join_token=node_routes.issue_join_token,
        inspect_join_token=node_routes.inspect_join_token,
        join=node_routes.join,
        node_heartbeat=node_routes.node_heartbeat,
        node_health=node_routes.node_health,
        operation_plan=operation_routes.operation_plan,
        create_join_command=join_routes.create_join_command,
        JOB_ROLES=account_routes.JOB_ROLES,
        account_jobs=account_routes.account_jobs,
        lock_machine_capacity=account_routes.lock_machine_capacity,
        machine_capacity=account_routes.machine_capacity,
        save_managed_account=account_routes.save_managed_account,
        add_managed_account=account_routes.add_managed_account,
        update_managed_account=account_routes.update_managed_account,
        restart_account_sign_in=account_routes.restart_account_sign_in,
        account_sign_in_details=account_routes.account_sign_in_details,
        report_account_sign_in_status=account_routes.report_account_sign_in_status,
        complete_account_sign_in=account_routes.complete_account_sign_in,
        remove_managed_account=account_routes.remove_managed_account,
        set_node_work_acceptance=admin_routes.set_node_work_acceptance,
        remove_managed_node=admin_routes.remove_managed_node,
        create_node_operation=operation_routes.create_node_operation,
        node_operations=operation_routes.node_operations,
        probe_project=probe_routes.probe_project,
        check_probe_token=probe_routes.check_probe_token,
        save_check_probe=probe_routes.save_check_probe,
        next_node_operation=agent_routes.next_node_operation,
        checkpoint_node_operation=agent_routes.checkpoint_node_operation,
        finish_node_operation=agent_routes.finish_node_operation)
