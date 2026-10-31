"""Control API: the single point nodes and workers talk to.

Every write to Jira or GitHub goes through here and is accepted only with a valid (run_id, fence) pair, so
fencing is enforced in exactly one place; every accepted or refused write is recorded in write_audit.
"""
from __future__ import annotations

import hashlib
import base64
import hmac
import ipaddress
import re
import logging
import os
import secrets
import shlex
import socket
import time
import threading
import json
import httpx
from contextvars import ContextVar
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (JSONResponse)
from pydantic import BaseModel, Field

from .. import VERSION
from ..core.controlrepo import ControlRepo
from ..core.services import normalize_services
from . import (dashboard_timeout_routes, eligibility, jira_connect, licensing, runtime_settings, setup_wizard,
               shared_accounts, state_schema, support_bundle, system_check_api)
from . import capacity
from .route_deps import github_connection_used
from .github import GitHubApp
from .github_connect import GitHubSecretStore
from .github_registry import github_connections as _github_registry
from .github_connections import DEFAULT_ID, Connection, GitHubConnections, GitHubTokenApp
from . import inventory
from .application_context import Ctx
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
from .features.work.constants import utcnow

log = logging.getLogger("aiflow4.api")

MASTER_SCOPE = "master:global"
MASTER_CYCLE_S = 60
DEFAULT_MASTER_DAILY_BUDGET_MINUTES = 60
OWNER_TOKEN_PRINCIPAL = "owner-token"
HUMAN_QUEUE_WAIT_S = 90  # an approve click waits this long for its turn in the merge queue
HUMAN_QUEUE_POLL_S = 2
RELEASES = Path("/srv/aiflow/control/releases")
_NON_ROUTABLE_JOIN_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",
))
STATE_RESPONSE_SCHEMA = state_schema.STATE_RESPONSE_SCHEMA

def validated_join_control_url(value: str) -> str:
    """Validate the operator-configured API address advertised to a new machine."""
    control = str(value or "").strip()
    if not control or "\\" in control or any(ord(char) < 32 or char.isspace() for char in control):
        raise ValueError("join control URL is not configured")
    try:
        parsed = urlsplit(control)
        host = parsed.hostname or ""
        # Accessing .port validates malformed ports as well.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("join control URL is invalid") from exc
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or "?" in control or "#" in control):
        raise ValueError("join control URL must be an HTTP(S) URL without credentials, query, or fragment")
    normalized_host = host.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise ValueError("join control URL must not point to localhost")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            # Some URL stacks accept abbreviated or integer-form IPv4 literals; normalize those before checking.
            address = ipaddress.ip_address(socket.inet_aton(normalized_host))
        except (OSError, ValueError):
            address = None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address and (address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast
                    or address.is_reserved or any(address.version == net.version and address in net
                                                  for net in _NON_ROUTABLE_JOIN_NETWORKS)):
        raise ValueError("join control URL must use an address reachable from joining nodes")
    return control.rstrip("/")


def join_bootstrap_command(token: str, control: str) -> str:
    """Return the documented single-command bootstrap for a five-minute, node-bound join token."""
    installer = shlex.quote(f"{control.rstrip('/')}/v1/install.sh")
    return f"curl -fsSL {installer} | sudo sh -s -- --token {shlex.quote(token)}"




def github_connections(ctx: Ctx) -> GitHubConnections:
    """The registry of the connections after the default one (see `github_registry`)."""
    return _github_registry(ctx, lambda directory, org: GitHubApp(directory, org),
                            lambda token, account, kind: GitHubTokenApp(token, account, kind))


def github_connection_list(ctx: Ctx) -> list[Connection]:
    """Every connection in order, the legacy single App first."""
    out: list[Connection] = []
    default = github_gateway(ctx)
    if default is not None:
        meta = getattr(default, "meta", {}) or {}
        out.append(Connection(DEFAULT_ID, str(meta.get("name") or "GitHub App"), "app",
                              str(getattr(default, "org", "") or ctx.settings.github_org),
                              str(meta.get("account_type") or ""), default))
    return out + github_connections(ctx).connections()


def github_route(ctx: Ctx, repo: str) -> Connection:
    """The connection that serves a repository: its assignment, else the first of its owner that reaches it.

    Refuses with 409 when the assigned connection is gone or cannot reach the repository, and when none can."""
    registry = github_connections(ctx)
    connections = github_connection_list(ctx)
    if not connections:
        raise HTTPException(503, "GitHub is not connected")
    owner = repo.split("/", 1)[0].lower() if "/" in repo else ""
    assigned = registry.assignments().get(repo.lower())
    if assigned:
        chosen = next((c for c in connections if c.id == assigned), None)
        if chosen is None or not registry.reaches(chosen.id, chosen.gateway, repo):
            raise HTTPException(409, f"the GitHub connection assigned to {repo} cannot reach it")
        return chosen
    for conn in connections:
        if (not owner or conn.account.lower() == owner) and registry.reaches(conn.id, conn.gateway, repo):
            return conn
    raise HTTPException(409, f"no GitHub connection can reach {repo}")


def github_gateway(ctx: Ctx, repo: str | None = None) -> GitHubApp | None:
    """Load the shared secret-store configuration lazily and refresh it across API replicas.

    With a repository, always check that the selected connection can reach it, including a single legacy App.
    The connection used is remembered for the audit log."""
    if repo is not None:
        conn = github_route(ctx, repo)
        github_connection_used.set(conn.id)
        return conn.gateway
    gateway = ctx.github
    if gateway is None:
        store = GitHubSecretStore(ctx.settings.github_app_dir)
        if store.active_exists:
            gateway = GitHubApp(ctx.settings.github_app_dir, ctx.settings.github_org)
            ctx.github = gateway
    elif hasattr(gateway, "_refresh_config"):
        gateway._refresh_config()
        if not getattr(gateway, "configured", True):
            ctx.github = None
            gateway = None
    if gateway is not None and getattr(gateway, "org", ""):
        ctx.settings.github_org = str(gateway.org)
    github_connection_used.set(DEFAULT_ID if gateway is not None else "")
    return gateway


def github_gateway_for_connection(ctx: Ctx, connection_id: str, repo: str) -> GitHubApp:
    """Resolve the connection captured by a run and confirm it still reaches the repository."""
    registry = github_connections(ctx)
    connection = next((item for item in github_connection_list(ctx) if item.id == connection_id), None)
    if connection is None or not registry.reaches(connection.id, connection.gateway, repo):
        raise HTTPException(409, f"the GitHub connection assigned to {repo} cannot reach it")
    github_connection_used.set(connection.id)
    return connection.gateway


def github_control_gateway(ctx: Ctx) -> GitHubApp:
    """Resolve the connection assigned to the control repository, preserving per-repository routing."""
    repository = f"{ctx.settings.github_org}/{Path(ctx.settings.control_repo).name}"
    gateway = github_gateway(ctx, repository)
    if gateway is None:
        raise HTTPException(503, "GitHub is not connected")
    return gateway


def declare_project_services(ctx: Ctx, key: str, services: list[str], message: str) -> tuple[list[str], str]:
    """Write the project's test services (`services`, the only setting; `databases` is dropped) to its project file in
    aiflow-control and reload it. Returns the saved services and the commit. Used by the Projects screen and by the
    flow doctor (docs/v4/test-services.md)."""
    names = normalize_services(services)
    project_file = ctx.repo().root / "projects" / f"{key}.yaml"
    config = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
    config.pop("databases", None)
    if names:
        config["services"] = names
    else:
        config.pop("services", None)
    control_name = Path(ctx.settings.control_repo).name
    github = github_control_gateway(ctx)
    branch = github.default_branch(control_name)
    commit = github.commit_file(control_name, branch, f"projects/{key}.yaml",
                                yaml.safe_dump(config, sort_keys=False, allow_unicode=True), message)
    if ctx.refresh_repo:
        ctx.refresh_repo()
    return names, commit or ""


def github_context_for_repository(ctx: Ctx, repo: str) -> tuple[GitHubApp, str]:
    gateway = github_gateway(ctx, repo)
    if gateway is None:
        raise HTTPException(503, "GitHub is not connected")
    return gateway, github_connection_used.get()


def _git_proxy_key(ctx: Ctx) -> bytes:
    """Load or create the shared signing key for short-lived worker Git relay capabilities."""
    path = Path(ctx.settings.github_app_dir) / "worker-git-proxy.key"
    try:
        value = path.read_bytes()
    except FileNotFoundError:
        value = b""
    if value and len(value) < 32:
        raise RuntimeError("Git transfer signing key is invalid")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if not value:
        value = secrets.token_bytes(32)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # atomic create: a second API replica reads a complete key
            except FileExistsError:
                value = path.read_bytes()
        finally:
            temporary.unlink(missing_ok=True)
    if len(value) < 32:
        raise RuntimeError("Git transfer signing key is invalid")
    os.chmod(path, 0o600)
    return value


def _issue_git_proxy_token(ctx: Ctx, claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(_git_proxy_key(ctx), payload, hashlib.sha256).digest()
    return payload.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def _verify_git_proxy_token(ctx: Ctx, token: str) -> dict[str, Any] | None:
    try:
        encoded, encoded_signature = token.split(".", 1)
        payload = encoded.encode()
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(_git_proxy_key(ctx), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(claims, dict) or int(claims.get("expires", 0)) < int(utcnow().timestamp()):
            return None
        return claims
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        return None


def _git_proxy_credential(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return ""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        _, separator, token = decoded.partition(":")
        return token if separator else ""
    except (ValueError, UnicodeDecodeError):
        return ""


def _receive_pack_target(body: bytes) -> set[str] | None:
    """Read Git receive-pack command pkt-lines; malformed or empty requests are refused."""
    targets: set[str] = set()
    offset = 0
    while offset < len(body):
        header = body[offset:offset + 4]
        if len(header) != 4:
            return None
        try:
            length = int(header, 16)
        except ValueError:
            return None
        offset += 4
        if length == 0:
            break
        if length < 4 or offset + length - 4 > len(body):
            return None
        packet = body[offset:offset + length - 4]
        offset += length - 4
        fields = packet.split(b"\0", 1)[0].strip().split()
        if len(fields) >= 3 and re.fullmatch(rb"[0-9a-fA-F]{40,64}", fields[0]) and re.fullmatch(
                rb"[0-9a-fA-F]{40,64}", fields[1]):
            if set(fields[1]) == {ord("0")}:
                return None
            targets.add(fields[2].decode("ascii", errors="ignore"))
    return targets or None


def _github_git_upstream(repository: str, suffix: str, method: str, params: list[tuple[str, str]],
                         headers: dict[str, str], body: bytes, token: str) -> httpx.Response:
    """Relay one Git smart HTTP request using a Command Center held access token."""
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        return client.request(method, f"https://github.com/{repository}{suffix}", params=params,
                              headers=headers, content=body, auth=("x-access-token", token))


# --- request bodies ---------------------------------------------------------------------------------------


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
