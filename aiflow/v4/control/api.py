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
import uuid
import json
import httpx
from contextvars import ContextVar
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (JSONResponse)
from pydantic import BaseModel, Field

from .. import VERSION
from ..core.controlrepo import ControlRepo
from ..core import work_limits
from ..core.services import normalize_services
from . import (dashboard_timeout_routes, eligibility, jira_connect, knowledge, licensing, rollout, runtime_settings, setup_wizard,
               shared_accounts, state_schema, support_bundle, system_check_api, version_release)
from .console_auth import (ConsoleUser, COOKIE_NAME, has_permission,
                           resolve_session_user,
                           verify_session)
from . import capacity, merge_queue
from . import merge_flow
from . import quota as quota_reporting
from .incidents import flow_bug_candidates, suggested_flow_fix
from . import route_deps
from .db import J
from .route_deps import github_connection_used, h
from .github import GitHubApp
from .github_connect import GitHubSecretStore
from .github_registry import github_connections as _github_registry
from .github_connections import DEFAULT_ID, Connection, GitHubConnections, GitHubTokenApp
from . import inventory
from .application_context import Ctx
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
from .features.work.listeners import run_finished

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
    _identity: list[tuple[str, str, str]] = []

    def bot_identity() -> tuple[str, str]:
        github = github_gateway(ctx)
        if not github:
            return "aiflow4", "aiflow4@localhost"
        app_id = str((getattr(github, "meta", {}) or {}).get("id") or "")
        if not _identity or _identity[0][0] != app_id:
            name, email = github.bot_identity()
            _identity[:] = [(app_id, name, email)]
        return _identity[0][1], _identity[0][2]

    # --- auth -----------------------------------------------------------------------------------------------
    def node_auth(authorization: str = Header(default="")) -> str:
        return route_deps.node_auth(ctx, authorization)

    def release_reader(authorization: str = Header(default="")) -> str:
        """A node token, or an unexpired, unconsumed join token so a new machine can fetch the signed release."""
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            row = ctx.db.one("SELECT node_name FROM node_join_tokens WHERE token_hash=%s AND used_at IS NULL"
                             " AND expires_at > now()", (h(token),))
            if row:
                return row["node_name"]
        return node_auth(authorization)

    def reviewer_auth(request: Request, authorization: str = Header(default="")) -> str:
        """The legacy reviewer bearer token, or a console session whose roles grant `review.actions`."""
        configured = ctx.settings.reviewer_token
        scheme, separator, token = authorization.partition(" ")
        if (configured and separator and scheme.lower() == "bearer" and token.strip()
                and secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            return "reviewer"
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if user:
            ticket_key = str(request.path_params.get("key", ""))
            row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket_key,)) if ticket_key else None
            in_scope = (user.project_keys is None or not row or row["project"] in user.project_keys)
            if has_permission(user.effective_roles, "review.actions") and in_scope:
                return user.subject
            audit("console", "access_denied", ticket_key, "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")
        if not configured and not ctx.settings.console_session_secret:
            raise HTTPException(503, "reviewer actions are not configured")
        raise HTTPException(401, "reviewer authorization required", headers={"WWW-Authenticate": "Bearer"})

    def owner_auth(request: Request, authorization: str = Header(default="")) -> str:
        def writable() -> None:
            try:
                licensing.require_writable(ctx.db, ctx.settings.license_public_key)
            except licensing.LicenseError as exc:
                raise HTTPException(423, str(exc)) from None

        configured = ctx.settings.owner_token
        scheme, separator, token = authorization.partition(" ")
        if (configured and separator and scheme.lower() == "bearer" and token.strip()
                and secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            writable()
            return OWNER_TOKEN_PRINCIPAL
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if user and has_permission(user.effective_roles, "knowledge.approve"):
            writable()
            return user.subject
        if not configured and not ctx.settings.console_session_secret:
            raise HTTPException(503, "owner actions are not configured")
        if user:
            audit("aiflow-control", "knowledge_approve", "", "", None, user.subject, False,
                  "not available for your role")
            raise HTTPException(403, "not available for your role")
        raise HTTPException(401, "owner authorization required", headers={"WWW-Authenticate": "Bearer"})

    def deploy_auth(authorization: str = Header(default="")) -> str:
        """The deploy scripts on the control machine sign in with the owner token (a bearer token, never a cookie)."""
        configured = ctx.settings.owner_token
        if not configured:
            raise HTTPException(503, "deploy access is not configured")
        scheme, separator, token = authorization.partition(" ")
        if (not separator or scheme.lower() != "bearer" or not token.strip()
                or not secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            raise HTTPException(401, "deploy authorization required", headers={"WWW-Authenticate": "Bearer"})
        return "deploy"

    def audit(target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str, accepted: bool,
              result: str, payload: dict | None = None) -> None:
        route_deps.audit(ctx, target, action, ticket, run_id, fence, actor, accepted, result, payload)

    def audit_in(connection, target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str,
                 accepted: bool, result: str, payload: dict | None = None) -> None:
        route_deps.audit_in(connection, target, action, ticket, run_id, fence, actor, accepted, result, payload)

    def console_user(request: Request, permissions: set[str] | str | None, target: str = "console", action: str = "read",
                     node: str = "") -> ConsoleUser:
        return route_deps.console_user(ctx, request, permissions, target, action, node)

    def optional_console_user(request: Request) -> ConsoleUser | None:
        return route_deps.optional_console_user(ctx, request)

    def require_capacity(resource: str, count: int | None = None, *, connection=None) -> dict:
        try:
            return licensing.require_capacity(ctx, resource, count, public_key=ctx.settings.license_public_key,
                                              connection=connection)
        except licensing.LicenseError as exc:
            raise HTTPException(409, str(exc)) from None

    def run_row(run_id: str) -> dict:
        return route_deps.run_row(ctx, run_id)

    def fenced(run_id: str, fence: int, target: str, action: str, payload: dict | None = None) -> dict:
        return route_deps.fenced(ctx, run_id, fence, target, action, payload)

    def master_run(run_id: str, fence: int, node: str, action: str, payload: dict | None = None) -> dict:
        run = fenced(run_id, fence, "master", action, payload)
        if run["step"] != "master":
            raise HTTPException(403, "only a master run can use this endpoint")
        if run["node"] != node:
            raise HTTPException(403, "master run belongs to another node")
        return run

    def reviewer_run(key: str, statuses: tuple[str, ...] = ("Human review",),
                     allowed_kinds: tuple[str, ...] | None = None) -> tuple[dict, str, int, int]:
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        ticket = ctx.db.one("SELECT key, project, status, labels, summary, blocked_by FROM tickets WHERE key=%s", (key,))
        if not ticket:
            raise HTTPException(404, "unknown ticket")
        if ticket["status"] not in statuses:
            raise HTTPException(409, "ticket is no longer waiting for this review action")
        if allowed_kinds is not None and github_fenced_routes.current_review_item_kind(ticket) not in allowed_kinds:
            raise HTTPException(409, "this action does not apply to the current human review item")
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        pr_run = ctx.db.one("SELECT pr_url,details FROM runs WHERE ticket_key=%s AND pr_url<>''"
                            " ORDER BY started_at DESC LIMIT 1", (key,))
        match = re.search(r"/pull/(\d+)(?:/|$)", (pr_run or {}).get("pr_url", ""))
        number = int(match.group(1)) if match else 0
        if number:
            repository = str(((pr_run or {}).get("details") or {}).get("repository") or "")
            project = ctx.repo().projects.get(ticket["project"])
            repository = repository or (repository_for_run(pr_run or {}, project) if project else "")
            if not project or not repository:
                raise HTTPException(404, "project repository is not configured")
            github_gateway(ctx, repository)
        run_id = str(uuid.uuid4())
        claim = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
        if not claim.ok:
            raise HTTPException(409, "ticket is already being changed")
        try:
            run_details = {"repository": str(((pr_run or {}).get("details") or {}).get("repository") or "")}
            ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,token_hash,details)"
                        " VALUES (%s,%s,%s,'qa','reviewer-console','human','control',%s,'',%s)",
                        (run_id, key, ticket["project"], claim.fence, J(run_details)))
        except Exception:
            ctx.leases.release(key, run_id, claim.fence)
            raise
        return ticket, run_id, claim.fence, number

    def finish_reviewer_run(key: str, run_id: str, fence: int, outcome: str, summary: str) -> None:
        ctx.db.run("UPDATE runs SET status='finished', outcome=%s, summary=%s, ended_at=now() WHERE id=%s",
                   (outcome, summary[:2000], run_id))
        try:
            knowledge.capture_reviewer_run(ctx.db, run_id, ctx.settings.jira_site, ticket_key=key, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 - learning must not block a review action
            log.warning("knowledge capture for run %s failed: %s", run_id, type(exc).__name__)
        ctx.leases.release(key, run_id, fence)
        run_finished(ctx, run_id=run_id, ticket_key=key, project=key.split("-", 1)[0], step="qa", fence=fence,
                     account="reviewer-console", provider="human", node="control", outcome=outcome, reason=summary[:200])

    def reviewer_comment(ticket: str, run_id: str, fence: int, number: int, text: str,
                         source: str = "human-review") -> None:
        """Mirror a reviewer action to Jira and, when the ticket has a PR, its PR conversation."""
        jira_run = fenced(run_id, fence, "jira", "comment", {"chars": len(text)})
        comment_id = ctx.jira.comment(ticket, text)
        audit("jira", "comment", ticket, run_id, fence, jira_run["account_id"], True, comment_id,
              {"source": source, "chars": len(text)})
        if number:
            github_run = fenced(run_id, fence, "github", "comment", {"number": number})
            project = ctx.repo().projects.get(github_run["project"])
            if not project:
                raise HTTPException(404, "project is not configured")
            repository = repository_for_run(github_run, project)
            gateway = github_gateway(ctx, repository)
            gateway.comment(repository.split("/", 1)[-1], number, text)
            audit("github", "comment", ticket, run_id, fence, github_run["account_id"], True, "ok",
                  {"source": source, "number": number, "repo": repository})

    def reviewer_transition(ticket: str, run_id: str, fence: int, target: str, reason: str) -> str:
        transition_run = fenced(run_id, fence, "jira", "transition", {"to": target})
        before = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (ticket,))
        status = ctx.jira.transition_to(ticket, target)
        ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN %s='Done' THEN coalesce(done_at,now())"
                   " ELSE done_at END, synced_at=now() WHERE key=%s", (status, status, ticket))
        merge_flow.record_move(ctx, ticket, (before or {}).get("status", ""), status, merge_flow.PERSON, reason)
        audit("jira", "transition", ticket, run_id, fence, transition_run["account_id"], True, status,
              {"to": target, "source": "human-review"})
        ctx.events.emit("jira.transition", ticket=ticket, project=transition_run["project"], run_id=run_id,
                        fence=fence, account="reviewer-console", node="control", outcome=status, reason=reason)
        return status

    def reviewer_labels(ticket: str, run_id: str, fence: int, add: tuple[str, ...] = (),
                        remove: tuple[str, ...] = ()) -> None:
        if not add and not remove:
            return
        fields_run = fenced(run_id, fence, "jira", "fields", {"add": list(add), "remove": list(remove)})
        for label in add:
            ctx.jira.add_label(ticket, label)
            ctx.db.run("UPDATE tickets SET labels=CASE WHEN %s=ANY(labels) THEN labels"
                       " ELSE array_append(labels,%s) END WHERE key=%s", (label, label, ticket))
        for label in remove:
            ctx.jira.remove_label(ticket, label)
            ctx.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, ticket))
        audit("jira", "fields", ticket, run_id, fence, fields_run["account_id"], True, "review labels updated",
              {"source": "human-review", "add": list(add), "remove": list(remove)})

    def record_reviewer_action(ticket: dict, run_id: str, fence: int, action: str, text: str = "") -> None:
        latest_dev = ctx.db.one("SELECT step,outcome FROM runs WHERE ticket_key=%s AND step='dev'"
                                " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
        kind = github_fenced_routes.review_item_kind(ticket, latest_dev)
        audit("human-review", action, ticket["key"], run_id, fence, "reviewer-console", True, "accepted",
              {"kind": kind, "text": text[:4000]})
        ctx.events.emit("review.action", ticket=ticket["key"], project=ticket["project"], step="qa",
                        run_id=run_id, fence=fence, account="reviewer-console", node="control", outcome=action,
                        action=action, kind=kind, text=text[:4000])

    def repository_for_run(run: dict[str, Any], project=None) -> str:
        """Use the repository captured when the run was claimed, with a legacy settings fallback."""
        saved = str(((run.get("details") or {}).get("repository") or ""))
        if saved:
            return saved
        project = project or ctx.repo().projects.get(run.get("project"))
        if not project:
            return ""
        ticket = ctx.db.one("SELECT key,type,parent_key FROM tickets WHERE key=%s", (run.get("ticket_key"),)) or {}
        ticket["epic"] = projects_routes.ticket_epic(ticket)
        return project.repo_for(ticket)

    def settings_for_run(run: dict[str, Any], project) -> dict[str, Any]:
        settings = (run.get("details") or {}).get("repository_settings")
        if isinstance(settings, dict) and settings.get("repo"):
            return settings
        return project.settings_for_repo(repository_for_run(run, project))

    def matches_run_repository(repository: str, requested: str) -> bool:
        requested = requested.strip().lower()
        return requested in {repository.lower(), repository.split("/", 1)[-1].lower()}

    def merge_command(run: dict, number: int, title: str = "", method: str = "squash", node: str = "") -> dict:
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        repo_name = repository.split("/", 1)[-1]
        res = github.merge_pr(repo_name, number, title, method)
        ok = bool(res.get("merged"))
        audit("github", "merge", run["ticket_key"], str(run["id"]), run["fence"], run["account_id"], ok,
              res.get("sha") or res.get("message", ""), {"number": number, "method": method,
                                                             "repo": repository})
        ctx.events.emit("github.merge", ticket=run["ticket_key"], run_id=str(run["id"]), fence=run["fence"],
                        account=run["account_id"], node=node or run["node"],
                        outcome="merged" if ok else "refused", reason=res.get("message", "")[:200])
        return res

    def queued_merge(run: dict, number: int, title: str, method: str, node: str) -> dict:
        """Merge a human-approved pull request through the merge queue, like a verify run does.

        The click waits (up to HUMAN_QUEUE_WAIT_S) for its turn behind pull requests that are ready earlier; a
        branch that had to be brought up to date is not merged on this click (its checks must run on the new head)
        and the reviewer approves again."""
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        repository = repository_for_run(run, project)
        deadline = time.monotonic() + HUMAN_QUEUE_WAIT_S
        while True:
            res = merge_queue.merge_next(ctx.db, github_gateway(ctx, repository), repository, number,
                                         run["ticket_key"], str(run["id"]),
                                         lambda: merge_command(run, number, title, method, node))
            if not res.get("queued") or time.monotonic() >= deadline:
                break
            time.sleep(HUMAN_QUEUE_POLL_S)
        if res.get("queued"):
            merge_queue.leave(ctx.db, repository, number)
        if not res.get("merged") and not res.get("message"):
            res["message"] = "the merge was refused"
        return res

    def master_budget_state() -> dict[str, Any]:
        setting = ctx.db.one("SELECT daily_budget_minutes FROM master_settings WHERE singleton=true") or {
            "daily_budget_minutes": DEFAULT_MASTER_DAILY_BUDGET_MINUTES}
        budget = int(setting["daily_budget_minutes"])
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        usage = ctx.db.one(
            "SELECT coalesce(sum(coalesce(nullif(r.details->>'model_time_s','')::numeric,"
            " extract(epoch FROM (coalesce(r.ended_at,now())-r.started_at))) / 60),0) AS used_minutes"
            " FROM runs r WHERE r.step='master' AND r.started_at >= %s", (day_start,)) or {}
        used = max(0.0, float(usage.get("used_minutes") or 0))
        remaining = max(0, int(budget * 60 - used * 60))
        return {"daily_budget_minutes": budget, "used_minutes": round(used, 1),
                "remaining_minutes": round(max(0, budget - used), 1), "remaining_seconds": remaining,
                "enabled": budget > 0 and remaining > 0}

    def account_master_paused(account_id: str) -> bool:
        return bool(ctx.db.one("SELECT 1 AS paused FROM master_account_pauses"
                               " WHERE account_id=%s AND paused_until > now()", (account_id,)))

    def mark_machine_auth_failure(account_id: str, node: str, summary: str) -> None:
        """Require sign-in again on the failing machine while keeping other assignments usable."""
        acc = ctx.repo().account(account_id)
        other_machine_ready = shared_accounts.mark_pending(ctx.db, acc, node) if acc else False
        active = bool(ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1",
                                 (account_id,)))
        cooling = ctx.leases.cooling(account_id)
        status = "cooldown" if cooling else "working" if active else "idle" if other_machine_ready else "signed_out"
        ctx.db.run("UPDATE accounts SET status=%s,last_error=%s,updated_at=now(),"
                   "idle_since=CASE WHEN %s='idle' THEN coalesce(idle_since,now()) ELSE NULL END WHERE id=%s",
                   (status, summary[:300], status, account_id))

    def quota_forecast_at_risk(account_id: str) -> bool:
        """Read the latest watchdog forecast without rebuilding weeks of run and probe history per poll."""
        row = ctx.db.one("SELECT quota_at_risk FROM accounts WHERE id=%s", (account_id,))
        return bool(row and row["quota_at_risk"])

    def refresh_quota_forecast(account_id: str, provider: str, quota: dict) -> dict:
        """Refresh one account's saved forecast after it reports new quota data."""
        awareness = quota_reporting.account_assessment(ctx.db, account_id, provider, quota or {})
        ctx.db.run("UPDATE accounts SET quota_at_risk=%s,quota_forecast=%s WHERE id=%s",
                   (bool(awareness.get("at_risk")), J(awareness), account_id))
        return awareness

    def quota_peer_accounts(account_id: str, roles: set[str] | None = None) -> list[dict]:
        """Ready same-provider accounts that can serve at least one of this account's workflow roles."""
        account_row = ctx.db.one("SELECT provider FROM accounts WHERE id=%s", (account_id,))
        if not account_row:
            return []
        peers = ctx.db.all(
            "SELECT a.id,a.roles,a.status,a.quota_at_risk FROM accounts a"
            " WHERE a.provider=%s AND a.id<>%s AND a.status NOT IN ('cooldown','signed_out','error') ORDER BY a.id",
            (account_row["provider"], account_id))
        online = shared_accounts.online_nodes(ctx.db)
        available: list[dict] = []
        for peer in peers:
            account = ctx.repo().account(peer["id"])
            peer["nodes"] = ([machine for machine in account.machines if machine in online
                               and rollout.node_compatible(ctx, machine, RELEASES)["compatible"]
                               and shared_accounts.may_take(ctx.db, account, machine, as_peer=True)[0]]
                              if account else [])
            if not account or account.paused or not peer["nodes"]:
                continue
            peer_roles = set(peer.get("roles") or ["dev", "verify"])
            if roles and not roles.intersection(peer_roles):
                continue
            if roles == {"master"} and peer["status"] not in ("idle", "working", "probing"):
                continue
            if peer["quota_at_risk"] or ctx.leases.cooling(peer["id"]) or account_master_paused(peer["id"]):
                continue
            available.append(peer)
        return available

    def account_quota_at_risk(account_id: str, roles: set[str] | None = None) -> bool:
        """Defer a risky account only while another ready account of the same provider can take its role."""
        if not quota_forecast_at_risk(account_id):
            return False
        return bool(quota_peer_accounts(account_id, roles))

    def quota_peer_release_candidates(account_id: str) -> set[tuple[str, str, str]]:
        """Release work a ready peer can actually claim for this quota-risky account."""
        if not quota_forecast_at_risk(account_id):
            return set()
        available: set[tuple[str, str, str]] = set()
        for peer in quota_peer_accounts(account_id, {"master", "release"}):
            for node in peer["nodes"]:
                if not rollout.node_compatible(ctx, node, RELEASES)["compatible"]:
                    continue
                for candidate in version_release.candidates(ctx, peer["id"], node):
                    available.add((candidate["project"], str(candidate["version_id"]),
                                   str(candidate.get("repository") or "")))
        return available

    def master_snapshot() -> dict:
        now = utcnow()
        repo = ctx.repo()
        incidents = ctx.db.all("SELECT id, opened_at, severity, kind, subject, title, detail FROM incidents"
                               " WHERE closed_at IS NULL ORDER BY opened_at")
        needs_owner = ctx.db.all("SELECT key, project, summary, description, priority, labels, jira_updated,"
                                 " ('owner-escalated'=ANY(labels)) AS owner_escalated FROM tickets"
                                 " WHERE status='Needs owner'"
                                 " ORDER BY priority, jira_created NULLS LAST, key")
        running = ctx.db.all("SELECT id, ticket_key, project, step, account_id, provider, model, node, fence, started_at"
                             " FROM runs WHERE status='running' AND step<>'master' ORDER BY started_at")
        clocks = []
        for run in running:
            project = repo.projects.get(run["project"])
            limits = project.work_minutes() if project else dict(work_limits.DEFAULTS)
            elapsed = max(0, (now - run["started_at"]).total_seconds() / 60)
            clocks.append({"run_id": str(run["id"]), "ticket": run["ticket_key"], "project": run["project"],
                           "step": run["step"], "account": run["account_id"], "provider": run["provider"],
                           "model": run["model"], "node": run["node"], "fence": run["fence"],
                           "started_at": run["started_at"].isoformat(), "elapsed_minutes": round(elapsed, 1),
                           "work_minutes": limits,
                           "passed": [name for name in ("target", "warn", "hard") if elapsed >= limits[name]]})
        offline_nodes = ctx.db.all("SELECT name, last_heartbeat FROM nodes WHERE status='offline' ORDER BY name")
        quota_incidents = [i for i in incidents if i["kind"] in ("account_quota", "quota_high")]
        high_subjects = {i["subject"].split(":", 1)[0] for i in quota_incidents if i["kind"] == "quota_high"}
        quota_accounts = []
        for account in ctx.db.all("SELECT id, provider, node, status, cooldown_until, last_error, quota FROM accounts"):
            cooling = ctx.leases.cooling(account["id"])
            pause = ctx.db.one("SELECT paused_until, reason FROM master_account_pauses"
                               " WHERE account_id=%s AND paused_until > now()", (account["id"],))
            if (account["status"] in ("cooldown", "signed_out", "error") or cooling
                    or account["id"] in high_subjects or pause):
                quota_accounts.append({**account, "cooldown": {"reason": cooling[0], "seconds": cooling[1]}
                                      if cooling else None, "master_pause": pause})
        return {"scope": "global", "captured_at": now.isoformat(), "open_incidents": incidents,
                "needs_owner": needs_owner, "running_clocks": clocks, "offline_nodes": offline_nodes,
                "quota_accounts": quota_accounts, "quota_incidents": quota_incidents,
                "autonomy": master_budget_state()}

    def process_flow_bugs(run: dict) -> list[dict]:
        """Create or update detected TESTFLOW Bugs from this valid, fenced master cycle."""
        if not ctx.jira:
            return []
        reports: list[dict] = []
        terminal = {"done", "closed", "resolved", "cancelled", "canceled"}
        for candidate in flow_bug_candidates(ctx):
            tracker = ctx.db.one("SELECT ticket_key, run_ids, repeat_count, in_intake FROM flow_bug_tracker"
                                 " WHERE signature=%s", (candidate["signature"],))
            if tracker:
                ticket_key = tracker["ticket_key"]
                try:
                    status = ctx.jira.status(ticket_key)
                except Exception as exc:  # Jira reads can fail transiently; retry on the next master cycle.
                    log.warning("could not check status of flow-bug %s: %s", ticket_key, type(exc).__name__)
                    continue
                ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, ticket_key))
                if status.casefold() not in terminal:
                    if not tracker["in_intake"]:  # created earlier, but the move to Intake failed: retry only that
                        if status == "Intake":
                            ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now()"
                                       " WHERE signature=%s", (candidate["signature"],))
                        elif move_flow_bug_to_intake(run, ticket_key, candidate["signature"]):
                            reports.append({"action": "moved_to_intake", "ticket": ticket_key,
                                            "signature": candidate["signature"]})
                    known = set(tracker["run_ids"] or [])
                    new_runs = [r for r in candidate["runs"] if r["id"] not in known]
                    if new_runs:
                        repeat_count = int(tracker["repeat_count"]) + len(new_runs)
                        total = len(known) + len(new_runs)
                        body = ("AI Flow · master · repeated flow error\n"
                                f"Pattern: {candidate['pattern']}\n"
                                f"Repeat count: {repeat_count} additional failed runs since this Bug was opened "
                                f"({total} matching runs recorded).\n"
                                f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                                "New run IDs: " + ", ".join(r["id"] for r in new_runs))
                        comment_id = ctx.jira.comment(ticket_key, body)
                        all_ids = sorted(known | {r["id"] for r in new_runs})
                        ctx.db.run("UPDATE flow_bug_tracker SET run_ids=%s, repeat_count=%s, updated_at=now()"
                                   " WHERE signature=%s", (all_ids, repeat_count, candidate["signature"]))
                        audit("jira", "flow_bug_repeat", ticket_key, str(run["id"]), run["fence"],
                              run["account_id"], True, comment_id,
                              {"signature": candidate["signature"], "repeat_count": repeat_count,
                               "new_run_ids": [r["id"] for r in new_runs]})
                        ctx.events.emit("flow_bug.repeated", ticket=ticket_key, project="TESTFLOW",
                                        run_id=str(run["id"]), fence=run["fence"], account=run["account_id"],
                                        node=run["node"], outcome="commented", reason=candidate["pattern"],
                                        data={"repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                        reports.append({"action": "repeated", "ticket": ticket_key,
                                        "repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                    continue

            if not candidate["creation_eligible"]:
                # The tracker keeps low-volume recurrences visible for open Bugs, but a closed Bug only
                # gets replaced after the normal repetition threshold or an aged ticket_stuck incident.
                continue

            run_lines = [f"- {item['id']} · {item['ticket']} · {item['step']} · node {item['node']} · release {item['release']}"
                         for item in candidate["runs"]]
            reason = ("at least three matching failed runs across at least two TESTFLOW tickets in 30 minutes"
                      if len(candidate["runs"]) >= 3 and len(candidate["tickets"]) >= 2 else
                      "a ticket_stuck incident for this error pattern remained open over 30 minutes")
            description = ("Automatically detected by the AI Flow master cycle.\n\n"
                           f"Fingerprint: {candidate['signature']}\n"
                           f"Pattern: {candidate['pattern']}\n"
                           f"Error kind: {candidate['error_kind']}\n"
                           f"Detection: {reason}.\n"
                           f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                           f"Steps: {', '.join(candidate['steps'])}\n"
                           f"Nodes: {', '.join(candidate['nodes'])}\n"
                           f"AI Flow releases: {', '.join(candidate['releases'])}\n"
                           "Related runs:\n" + "\n".join(run_lines) + "\n\n"
                           f"Suggested fix: {suggested_flow_fix(candidate)}")
            summary = f"[Flow bug] {candidate['pattern'].splitlines()[0]}"[:240]
            try:
                ticket_key = ctx.jira.create_issue("TESTFLOW", summary, description, issuetype="Bug",
                                                   labels=["flow-bug"])
            except Exception as exc:  # Keep the monitoring snapshot available and retry detection next cycle.
                audit("jira", "flow_bug_create", "", str(run["id"]), run["fence"], run["account_id"], False,
                      "Jira issue creation failed", {"signature": candidate["signature"], "error": type(exc).__name__})
                log.warning("could not create flow-bug for signature %s: %s",
                            candidate["signature"], type(exc).__name__)
                continue
            # Record the new Bug before any further Jira call, so a failure below never leads to a second Bug for
            # this signature. The status is refreshed from Jira on the next cycle; it is not Intake until moved.
            run_ids = sorted(item["id"] for item in candidate["runs"])
            ctx.db.run("INSERT INTO flow_bug_tracker (signature, ticket_key, run_ids, repeat_count, in_intake)"
                       " VALUES (%s,%s,%s,0,false) ON CONFLICT (signature) DO UPDATE SET ticket_key=EXCLUDED.ticket_key,"
                       " run_ids=EXCLUDED.run_ids, repeat_count=0, in_intake=false, updated_at=now()",
                       (candidate["signature"], ticket_key, run_ids))
            ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                       " VALUES (%s,'TESTFLOW','Created','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                       " type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                       " description=EXCLUDED.description, synced_at=now()",
                       (ticket_key, ["flow-bug"], summary, description))
            audit("jira", "flow_bug_create", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
                  "created", {"reason": reason, "signature": candidate["signature"], "run_ids": run_ids,
                              "tickets": candidate["tickets"], "labels": ["flow-bug"]})
            status = move_flow_bug_to_intake(run, ticket_key, candidate["signature"])
            ctx.events.emit("flow_bug.created", ticket=ticket_key, project="TESTFLOW", run_id=str(run["id"]),
                            fence=run["fence"], account=run["account_id"], node=run["node"],
                            outcome=status or "created", reason=candidate["pattern"],
                            data={"signature": candidate["signature"], "run_ids": run_ids,
                                  "tickets": candidate["tickets"]})
            reports.append({"action": "created", "ticket": ticket_key, "signature": candidate["signature"],
                            "run_ids": run_ids, "in_intake": status == "Intake"})
        return reports

    def move_flow_bug_to_intake(run: dict, ticket_key: str, signature: str) -> str | None:
        """Move a tracked flow-bug to Intake once; returns "Intake", or None when the move failed (retried later)."""
        try:
            status = ctx.jira.transition_to(ticket_key, "Intake")
            if status != "Intake":
                raise RuntimeError("flow-bug did not reach Intake")
        except Exception as exc:  # The tracker row keeps the key; the next master cycle retries only the move.
            audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], False,
                  "Intake transition failed", {"signature": signature, "error": type(exc).__name__})
            log.warning("could not move flow-bug %s to Intake: %s", ticket_key, type(exc).__name__)
            return None
        ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now() WHERE signature=%s", (signature,))
        ctx.db.run("UPDATE tickets SET status='Intake', synced_at=now() WHERE key=%s", (ticket_key,))
        audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
              "Intake", {"signature": signature})
        return "Intake"

    # --- nodes ----------------------------------------------------------------------------------------------

    def create_join_grant(name: str, role: str, project: str) -> tuple[str, datetime]:
        """Create the shared five-minute grant used by both Command Center entry points."""
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise HTTPException(422, "invalid node name")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role):
            raise HTTPException(422, "invalid node role")
        repo = ctx.repo()
        if project not in repo.projects:
            raise HTTPException(422, "unknown project")
        dynamic = repo.inventory_in_db or name not in repo.nodes
        if not dynamic and project not in repo.nodes[name].projects:
            raise HTTPException(422, "project is not assigned to this node in the control repo")
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                # The count, join grant, and pending node insert share the lock and transaction. A second request
                # waits here, then sees the first request's node before checking the last available slot.
                licensing.lock_resource_admission(conn)
                existing = conn.execute(
                    "SELECT name,removed_at,(token_hash<>'' AND last_heartbeat > now()-interval '75 seconds') AS live"
                    " FROM nodes WHERE name=%s FOR UPDATE", (name,)).fetchone()
                if existing and existing["live"] and not existing["removed_at"]:
                    raise HTTPException(409, "a live node already uses this name")
                if not existing or existing["removed_at"]:
                    count = int(conn.execute(
                        "SELECT count(*) AS count FROM nodes WHERE removed_at IS NULL").fetchone()["count"])
                    require_capacity("machines", count, connection=conn)
                token = secrets.token_urlsafe(32)
                issued = conn.execute("INSERT INTO node_join_tokens (token_hash,node_name,role,project,dynamic,expires_at)"
                                      " VALUES (%s,%s,%s,%s,%s,now()+interval '5 minutes')"
                                      " ON CONFLICT (node_name) WHERE used_at IS NULL DO UPDATE SET"
                                      " token_hash=EXCLUDED.token_hash,role=EXCLUDED.role,project=EXCLUDED.project,"
                                      " dynamic=EXCLUDED.dynamic,created_at=now(),expires_at=EXCLUDED.expires_at"
                                      " RETURNING expires_at",
                                      (h(token), name, role, project, dynamic)).fetchone()
                # Register a pending dynamic node so Command Center can show it before its first heartbeat.
                conn.execute("INSERT INTO nodes (name,role,status,projects,dynamic,accepting_work,removed_at)"
                             " VALUES (%s,%s,'joined',%s,%s,true,NULL) ON CONFLICT (name) DO UPDATE"
                             " SET removed_at=NULL,accepting_work=true WHERE nodes.removed_at IS NOT NULL",
                             (name, role, [project], dynamic))
        return token, issued["expires_at"]

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
    return app
