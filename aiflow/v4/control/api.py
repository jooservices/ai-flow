"""Control API: the single point nodes and workers talk to.

Every write to Jira or GitHub goes through here and is accepted only with a valid (run_id, fence) pair, so
fencing is enforced in exactly one place; every accepted or refused write is recorded in write_audit.
"""
from __future__ import annotations

import logging
import time
import threading
from contextvars import ContextVar
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (JSONResponse)
from pydantic import BaseModel, Field

from .. import VERSION
from ..core.controlrepo import ControlRepo
from . import (dashboard_timeout_routes, eligibility, jira_connect, licensing, runtime_settings, setup_wizard,
               shared_accounts, support_bundle, system_check_api)
from . import capacity
from . import inventory
from .application_context import Ctx
from .gateway_helpers import github_context_for_repository, github_control_gateway
from . import app_helpers
from .features.work_routes import routes as work_routes_module
from .features.github_ci_log import routes as github_ci_log_module
from .features.runs import routes as runs_module
from .features.github_fenced import routes as github_fenced_module
from .features.business import routes as business_module
from .features.scale_proof import routes as scale_proof_module
from .features.knowledge import routes as knowledge_module
from .features.backups import routes as backups_module
from .features.project_report import routes as project_report_module
from .features.console import routes as console_module
from .features.release import routes as release_module
from .features.version_release import routes as version_release_module
from .features.fleet import routes as fleet_module
from .features.projects import routes as projects_module
from .features.projects.bodies import ProjectPreflightBody

log = logging.getLogger("aiflow4.api")

MASTER_SCOPE = "master:global"
MASTER_CYCLE_S = 60
DEFAULT_MASTER_DAILY_BUDGET_MINUTES = 60
OWNER_TOKEN_PRINCIPAL = "owner-token"
HUMAN_QUEUE_WAIT_S = 90  # an approve click waits this long for its turn in the merge queue
HUMAN_QUEUE_POLL_S = 2
RELEASES = Path("/srv/aiflow/control/releases")
HeartbeatBody = system_check_api.HeartbeatBody


class ProjectCreateBody(ProjectPreflightBody):
    services: list[str] = Field(default_factory=list, max_length=10)
    checks: list[str] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=5, ge=0, le=100)
    parallel_limit: int = Field(default=2, ge=1, le=100)
    merge_method: Literal["merge", "squash", "rebase"] = "squash"




class ProbeBody(BaseModel):
    ok: bool
    error_kind: str = ""
    reset_at: datetime | None = None
    quota: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = ""
    credential_expires_at: datetime | None = None


# --- app --------------------------------------------------------------------------------------------------
def create_app(ctx: Ctx) -> FastAPI:
    from contextlib import asynccontextmanager

    ctx.worker_resource_counts = licensing.ResourceCountsCache(ctx)
    runtime_repo_cache: ContextVar[ControlRepo | None] = ContextVar("aiflow_runtime_repo_cache", default=None)
    runtime_repo_cache_enabled: ContextVar[bool] = ContextVar("aiflow_runtime_repo_cache_enabled", default=False)
    capacity_history_lock = threading.Lock()
    capacity_history_cache: dict[str, Any] = {"at": 0.0, "samples": []}

    def capacity_history() -> list[dict[str, Any]]:
        """Share the completed-work estimate sample across concurrent worker polls for one second."""
        with capacity_history_lock:
            now = time.monotonic()
            if now - float(capacity_history_cache["at"]) >= 1.0:
                capacity_history_cache["samples"] = capacity.history(ctx.db)
                capacity_history_cache["at"] = now
            return capacity_history_cache["samples"]

    configured_repo = ctx.configured_repo or ctx.repo
    ctx.configured_repo = configured_repo

    def runtime_repo() -> ControlRepo:
        """Merge console-managed accounts over the control-repo snapshot for live worker scheduling."""
        cache_enabled = runtime_repo_cache_enabled.get()
        cached = runtime_repo_cache.get() if cache_enabled else None
        if cached is not None:
            return cached
        base = configured_repo()
        if not inventory.needs_runtime_overlay(ctx.db):
            if cache_enabled:
                runtime_repo_cache.set(base)
            return base
        merged = shared_accounts.overlay(inventory.apply(base, ctx.db), ctx.db)
        if cache_enabled:
            runtime_repo_cache.set(merged)
        return merged

    ctx.repo = runtime_repo  # the registry is the live source for machines and accounts (inventory.py)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            licensing.initialize(ctx.db)
        except Exception as e:  # noqa: BLE001
            log.warning("licence state initialization failed (%s)", type(e).__name__)
        try:
            if not _app.state.sync_accounts():  # a licence-skipped repo account is imported on a later start
                _app.state.import_inventory()
        except Exception as e:  # noqa: BLE001
            log.warning("account sync failed: %s", e)
        yield

    app = FastAPI(title="AI Flow v4 control API", version=VERSION, lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def isolate_runtime_repo_snapshot(request: Request, call_next):
        """Reuse the live view only in routes that never change account/project configuration."""
        enabled = request.url.path in {
            "/v1/work/candidates", "/v1/work/claim",
            "/v1/version-releases/candidates", "/v1/version-releases/claim", "/v1/state",
        } or (request.method == "GET" and request.url.path == "/v1/projects")
        cache_token = runtime_repo_cache.set(None)
        enabled_token = runtime_repo_cache_enabled.set(enabled)
        try:
            return await call_next(request)
        finally:
            runtime_repo_cache_enabled.reset(enabled_token)
            runtime_repo_cache.reset(cache_token)

    support_bundle.control_log_buffer(
        *(v if isinstance(v, str) else "" for v in (ctx.settings.support_log_dir, ctx.settings.o2_source))
    )
    app.state.license_sync_warnings = []
    # Release reconciliation loops receive the same per-repository routing as API handlers.
    ctx.github_connection_for_repository = lambda repository: github_context_for_repository(ctx, repository)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Return safe 422 details without echoing rejected credentials or licence material."""
        if request.url.path in {"/v1/licensing/activate", "/v1/licensing/offline"}:
            return JSONResponse(
                {"detail": "Licence key or signed licence file is invalid or exceeds the supported size."},
                status_code=422,
            )
        errors = []
        for error in exc.errors():
            errors.append({"loc": list(error.get("loc") or ()), "msg": "Invalid value",
                           "type": error.get("type", "value_error")})
        return JSONResponse(status_code=422, content={"detail": errors})

    def sync_accounts() -> list[str]:
        """Sync configured accounts and machines that fit the current licence limits (until the one-time import)."""
        if inventory.is_imported(ctx.db):  # the database owns machines and accounts; the repo files are ignored
            app.state.license_sync_warnings = []
            return []
        repo = configured_repo()
        warnings: list[str] = []
        removed = {row["id"] for row in ctx.db.all(
            "SELECT id FROM managed_ai_accounts WHERE removed_at IS NOT NULL")}
        removed_nodes = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE removed_at IS NOT NULL")}
        for a in repo.accounts:
            if a.id in removed or a.node in removed_nodes:
                continue
            if not ctx.db.one("SELECT 1 FROM accounts WHERE id=%s", (a.id,)):
                try:
                    # Startup admits configured accounts in order; use registered rows for admission because the
                    # Licence screen's usage count also includes configured accounts that have not synced yet.
                    registered_count = int(ctx.db.one("SELECT count(*) AS count FROM accounts")["count"])
                    licensing.require_capacity(ctx, "ai_accounts", count=registered_count,
                                                public_key=ctx.settings.license_public_key)
                except licensing.LicenseError as exc:
                    message = f"Configured AI account {a.id} was not synced: {exc}"
                    warnings.append(message)
                    log.warning("%s", message)
                    continue
            ctx.db.run(
                "INSERT INTO accounts (id, provider, node, tier, roles) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider, node=EXCLUDED.node, tier=EXCLUDED.tier,"
                " roles=EXCLUDED.roles, updated_at=now()", (a.id, a.provider, a.node, a.tier, a.roles))
        for n in repo.nodes.values():
            removed_node = ctx.db.one("SELECT removed_at FROM nodes WHERE name=%s", (n.name,))
            if removed_node and removed_node["removed_at"]:
                continue
            if not removed_node:
                try:
                    licensing.require_capacity(ctx, "machines", public_key=ctx.settings.license_public_key)
                except licensing.LicenseError as exc:
                    message = f"Configured machine {n.name} was not synced: {exc}"
                    warnings.append(message)
                    log.warning("%s", message)
                    continue
            # Keep the IP a node reported at join: a restart must not replace where it was last seen with
            # the control repo value. Only a first insert (no join yet) takes the configured address.
            ctx.db.run("INSERT INTO nodes (name, ip, role, slots_total, projects) VALUES (%s,%s,%s,%s,%s)"
                       " ON CONFLICT (name) DO UPDATE SET role=EXCLUDED.role, slots_total=EXCLUDED.slots_total,"
                       " projects=EXCLUDED.projects, dynamic=false",
                       (n.name, n.ip, n.role, n.slots, n.projects))
        app.state.license_sync_warnings = warnings
        return warnings

    app.state.sync_accounts = sync_accounts
    def import_inventory() -> dict:
        """Move the control repo's machines and accounts into the database, once (docs/v4/inventory-in-database.md)."""
        if ctx.refresh_repo:
            ctx.refresh_repo()  # pull synchronously: the import must see the same revision the remote cleanup targets
        return inventory.import_and_clean(ctx.db, configured_repo(), lambda: github_control_gateway(ctx),
                                          Path(ctx.settings.control_repo).name)
    app.state.import_inventory = import_inventory

    late = SimpleNamespace()  # filled once the route installers have run; the helpers read it when called
    helpers = app_helpers.install(app, ctx, SimpleNamespace(late=late))
    (account_master_paused, account_quota_at_risk, audit, audit_in, bot_identity, console_user, create_join_grant,
    deploy_auth, fenced, finish_reviewer_run, mark_machine_auth_failure, master_budget_state, master_run,
    master_snapshot, matches_run_repository, merge_command, node_auth, optional_console_user, owner_auth,
    process_flow_bugs, queued_merge, quota_forecast_at_risk, quota_peer_accounts, quota_peer_release_candidates,
    record_reviewer_action, refresh_quota_forecast, release_reader, repository_for_run, require_capacity,
    reviewer_auth, reviewer_comment, reviewer_labels, reviewer_run, reviewer_transition, run_row, settings_for_run) = (
    helpers.account_master_paused, helpers.account_quota_at_risk, helpers.audit, helpers.audit_in,
    helpers.bot_identity, helpers.console_user, helpers.create_join_grant, helpers.deploy_auth, helpers.fenced,
    helpers.finish_reviewer_run, helpers.mark_machine_auth_failure, helpers.master_budget_state, helpers.master_run,
    helpers.master_snapshot, helpers.matches_run_repository, helpers.merge_command, helpers.node_auth,
    helpers.optional_console_user, helpers.owner_auth, helpers.process_flow_bugs, helpers.queued_merge,
    helpers.quota_forecast_at_risk, helpers.quota_peer_accounts, helpers.quota_peer_release_candidates,
    helpers.record_reviewer_action, helpers.refresh_quota_forecast, helpers.release_reader,
    helpers.repository_for_run, helpers.require_capacity, helpers.reviewer_auth, helpers.reviewer_comment,
    helpers.reviewer_labels, helpers.reviewer_run, helpers.reviewer_transition, helpers.run_row,
    helpers.settings_for_run)

    # --- Jira writes (fenced) ---------------------------------------------------------------------------------


    # --- policy hook ------------------------------------------------------------------------------------------

    # --- shared accounts: enable an account on machines, remove it, report the sign-in prompt ----------------------
    shared_accounts.install(app, ctx, console_user, audit, node_auth)

    # --- account specs, no-generation quota refresh and legacy probes -------------------------------------------
    @app.get("/v1/accounts/{account_id}/spec")
    def account_spec(account_id: str, node: str = Depends(node_auth)) -> dict:
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or not acc.runs_on(node):
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        model, effort = repo.model_for(acc.provider, acc.tier)
        return {"account": acc.id, "provider": acc.provider, "tier": acc.tier, "model": model, "effort": effort}


    jira_connect.mount(app, ctx); setup_wizard.install(app, ctx)
    runtime_settings.install(app, ctx, console_user, audit)
    app.state.control_api_auth = {"node_auth": node_auth, "console_user": console_user, "audit": audit,
                                  "mark_machine_auth_failure": mark_machine_auth_failure}
    from .login import install_login
    install_login(app, ctx)
    eligibility.install(ctx, RELEASES, lambda *a, **k: work_routes.eligible(*a, **k), quota_forecast_at_risk, quota_peer_accounts)
    system_check_api.register_routes(app, ctx, console_user, optional_console_user); dashboard_timeout_routes.install(app, ctx, console_user, optional_console_user)
    release_routes = release_module.install(app, ctx, SimpleNamespace(node_auth=node_auth, release_reader=release_reader))
    fleet_routes = fleet_module.install(app, ctx, SimpleNamespace(audit=audit, audit_in=audit_in, console_user=console_user, create_join_grant=create_join_grant, current_release=release_routes.current_release, fleet_release=release_routes.fleet_release, node_auth=node_auth, release_assets=release_routes.release_assets, require_capacity=require_capacity))
    projects_routes = projects_module.install(app, ctx, SimpleNamespace(audit=audit, console_user=console_user, fenced=fenced, node_auth=node_auth, optional_console_user=optional_console_user, require_capacity=require_capacity))
    version_release_module.install(app, ctx, SimpleNamespace(account_master_paused=account_master_paused, bot_identity=bot_identity, fenced=fenced, node_auth=node_auth, quota_peer_release_candidates=quota_peer_release_candidates))
    console_module.install(app, ctx, SimpleNamespace(audit=audit, audit_in=audit_in, console_user=console_user, optional_console_user=optional_console_user, require_capacity=require_capacity))
    project_report_module.install(app, ctx, SimpleNamespace())
    backups_module.install(app, ctx, SimpleNamespace(audit=audit, console_user=console_user))
    knowledge_module.install(app, ctx, SimpleNamespace(audit=audit, console_user=console_user, owner_auth=owner_auth))
    scale_proof_module.install(app, ctx, SimpleNamespace(audit=audit, owner_auth=owner_auth))
    business_module.install(app, ctx, SimpleNamespace(audit=audit, deploy_auth=deploy_auth, optional_console_user=optional_console_user, owner_auth=owner_auth))
    github_fenced_routes = github_fenced_module.install(app, ctx, SimpleNamespace(audit=audit, console_user=console_user, fenced=fenced, finish_reviewer_run=finish_reviewer_run, fleet_routes=fleet_routes, matches_run_repository=matches_run_repository, merge_command=merge_command, node_auth=node_auth, optional_console_user=optional_console_user, projects_routes=projects_routes, queued_merge=queued_merge, record_reviewer_action=record_reviewer_action, repository_for_run=repository_for_run, reviewer_auth=reviewer_auth, reviewer_comment=reviewer_comment, reviewer_labels=reviewer_labels, reviewer_run=reviewer_run, reviewer_transition=reviewer_transition, run_row=run_row, settings_for_run=settings_for_run))
    runs_module.install(app, ctx, SimpleNamespace(audit=audit, console_user=console_user, last_dev=lambda *a, **k: work_routes.last_dev(*a, **k), mark_machine_auth_failure=mark_machine_auth_failure, master_budget_state=master_budget_state, master_run=master_run, master_snapshot=master_snapshot, node_auth=node_auth, process_flow_bugs=process_flow_bugs, refresh_quota_forecast=refresh_quota_forecast, run_row=run_row))
    github_ci_log_module.install(app, ctx, SimpleNamespace(audit=audit, fenced=fenced, node_auth=node_auth, repository_for_run=repository_for_run))
    work_routes = work_routes_module.install(app, ctx, SimpleNamespace(account_master_paused=account_master_paused, account_quota_at_risk=account_quota_at_risk, bot_identity=bot_identity, capacity_history=capacity_history, master_budget_state=master_budget_state, node_auth=node_auth, projects_routes=projects_routes, quota_forecast_at_risk=quota_forecast_at_risk, quota_peer_accounts=quota_peer_accounts))
    late.github_fenced_routes, late.projects_routes = github_fenced_routes, projects_routes
    return app
