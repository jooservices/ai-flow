"""Control API: the single point nodes and workers talk to.

Every write to Jira or GitHub goes through here and is accepted only with a valid (run_id, fence) pair, so
fencing is enforced in exactly one place; every accepted or refused write is recorded in write_audit.
"""
from __future__ import annotations

import hashlib
import base64
import hmac
import csv
import copy
import io
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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse)
from pydantic import BaseModel, Field

from .. import VERSION
from ..core.controlrepo import ControlRepo
from ..core import backups as control_backups, work_limits
from ..core.services import normalize_services
from .audit import record_console_user_change as audit_user, record_console_user_roles_change as audit_user_roles
from . import (business, capacity_routing as capacity_routing_service, dashboard_timeout_routes, eligibility, escalations,
               jira_connect, knowledge, layouts, licensing, load_policy, load_policy_api,
               memory, quota_refresh, role_health, rollout, runtime_settings, scale_proof, setup_wizard,
               shared_accounts, state_schema, support_bundle, system_check_api, tooling, version_release)
from . import run_inbox as run_inbox_control
from . import triage_claim as triage_claim_service
from . import environment as environment_reports
from .console_auth import (CANONICAL_ROLES, ConsoleUser, COOKIE_NAME, ROLE_LABELS, ROLE_PERMISSIONS, has_permission,
                           issue_session, landing_page, normalize_role, normalize_roles, resolve_session_user,
                           verify_session)
from . import capacity, merge_queue, review_queue_api, worker_candidates
from . import merge_flow
from . import quota as quota_reporting
from . import projects as project_lifecycle
from .incidents import flow_bug_candidates, suggested_flow_fix
from . import route_deps
from .db import J
from .route_deps import github_connection_used, h
from .github import GitHubApp, GitHubError
from .github_connect import GitHubSecretStore
from .github_registry import github_connections as _github_registry
from .github_connections import DEFAULT_ID, Connection, GitHubConnections, GitHubTokenApp
from .policy import decide_master_action
from .report import ReportNotFound, build_report, render_csv, render_html, render_markdown
from . import inventory, notify, pr_comments
from .pr_comments import PRCommentBody
from .application_context import Ctx
from .stakeholder_screenshot import decode_screenshot
from .features.release import routes as release_module
from .features.version_release import routes as version_release_module
from .features.fleet import routes as fleet_module
from .features.projects import routes as projects_module
from .features.projects.bodies import ProjectPreflightBody
from .features.work.constants import BACKOFF_SQL, PRIORITY, utcnow
from .features.work.claim import claim as claim_work
from .features.work.listeners import run_finished
from .features.work.eligible import eligible as eligible_tickets

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


class ClaimBody(BaseModel):
    ticket: str
    account: str
    step: str = "dev"


class ScaleProofBody(BaseModel):
    release: str = Field("", max_length=200)
    source: str = Field("ci", max_length=60)
    report: dict[str, Any]


class ProjectCreateBody(ProjectPreflightBody):
    services: list[str] = Field(default_factory=list, max_length=10)
    checks: list[str] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=5, ge=0, le=100)
    parallel_limit: int = Field(default=2, ge=1, le=100)
    merge_method: Literal["merge", "squash", "rebase"] = "squash"




class FenceBody(BaseModel):
    fence: int
    progress: dict[str, Any] = Field(default_factory=dict)
    inbox_received: list[int] = Field(default_factory=list, max_length=100)
    inbox_included: list[int] = Field(default_factory=list, max_length=100)


class ResultBody(BaseModel):
    fence: int
    outcome: str
    error_kind: str = ""
    reset_at: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    branch: str = Field(default="", max_length=240)
    pr_url: str = ""
    head_sha: str = ""
    summary: str = ""
    quota: dict[str, Any] = Field(default_factory=dict)
    quota_observed_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class GitHubTokenBody(BaseModel):
    run_id: str
    fence: int
    repo: str
    branch: str = ""


class PRBody(BaseModel):
    run_id: str
    fence: int
    repo: str
    head: str
    base: str
    title: str
    body: str = ""
    draft: bool = True


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


class MergeBody(BaseModel):
    run_id: str
    fence: int
    number: int
    title: str = ""
    method: str = "squash"



class BranchCleanupBody(BaseModel):
    run_id: str
    fence: int
    branch: str


class ReviewerBody(BaseModel):
    comment: str = ""
    reason: str = ""
    question: str = ""
    answer: str = ""


class MasterActionBody(BaseModel):
    run_id: str
    fence: int
    action: str
    ticket: str = ""
    intent: str = ""
    category: str = ""  # infrastructure | secret | production_release: always escalated to the owner
    headline: str = "follow up"
    what: str = ""
    why: str = ""
    result: str = ""
    next: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    kind: str = "master_follow_up"
    subject: str = ""
    severity: str = "warning"
    title: str = ""
    detail: str = ""
    target_status: str = ""
    options: list[str] = Field(default_factory=list)
    observation_key: str = ""
    account_id: str = ""
    pause_minutes: int = 30
    target_run_id: str = ""
    signature: str = ""


class MasterFlowBugsBody(BaseModel):
    run_id: str
    fence: int


class MasterPlanOutcomeBody(BaseModel):
    run_id: str
    fence: int
    valid: bool


class MasterBudgetBody(BaseModel):
    daily_budget_minutes: int = Field(ge=0, le=1440)


class ChangeRequestBody(BaseModel):
    title: str = ""
    description: str = ""
    epic: str = ""


class CloseProjectBody(BaseModel):
    actor: str = "owner"


class KnowledgeBody(BaseModel):
    id: str = ""
    type: str
    title: str
    content: str
    actor: str
    archived: bool = False
    version: int | None = None


class KnowledgeApprovalBody(BaseModel):
    approved_by: str = "owner"


class KnowledgeRestoreBody(BaseModel):
    version: int
    actor: str


class ConsoleUserCreateBody(BaseModel):
    email: str
    display_name: str = ""
    roles: list[str]
    project_keys: list[str] | None = None


class ConsoleUserUpdateBody(BaseModel):
    roles: list[str]
    project_keys: list[str] | None = None


class ConsoleUserDisabledBody(BaseModel):
    disabled: bool


class ViewAsBody(BaseModel):
    role: str


class BackupConfigBody(BaseModel):
    schedule: Literal["daily", "weekly"] = "daily"
    destination_kind: Literal["local", "mounted", "s3"] = "local"
    destination_path: str = "/srv/aiflow/backups"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_region: str = "us-east-1"
    retention_days: int = Field(default=30, ge=1, le=3650)


class KnowledgeItemBody(BaseModel):
    project: str = Field(default="", max_length=32)
    scope: str = Field(default="project", max_length=16)
    epic: str = Field(default="", max_length=100)
    kind: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=30_000)
    actor: str = Field(default="", max_length=200)


class KnowledgeItemActionBody(BaseModel):
    actor: str = Field(default="", max_length=200)
    project: str = Field(default="", max_length=32)
    scope: str = Field(default="", max_length=16)
    epic: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=30_000)
    duplicate_id: int | None = None


class StakeholderVerdictBody(BaseModel):
    change_key: str
    target_key: str = ""
    environment: Literal["develop", "production"]
    verdict: Literal["expected", "not_expected"]
    sender: str = Field(min_length=1, max_length=200)
    release_commit: str = Field(default="", max_length=500)
    expected: str = Field(default="", max_length=10_000)
    actual: str = Field(default="", max_length=10_000)
    screenshot_data: str = Field(default="", max_length=7_000_000)
    screenshot_filename: str = Field(default="screenshot.png", max_length=255)


class LicenseKeyBody(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(min_length=1, max_length=2000)


class OfflineLicenseBody(BaseModel):
    model_config = {"extra": "forbid"}
    license_file: str = Field(min_length=1, max_length=50000)


class PlanLimitsBody(BaseModel):
    model_config = {"extra": "forbid"}
    plan: str
    limits: dict[str, int]


# --- app --------------------------------------------------------------------------------------------------
def create_app(ctx: Ctx) -> FastAPI:
    from contextlib import asynccontextmanager

    ctx.worker_resource_counts = licensing.ResourceCountsCache(ctx)
    runtime_repo_cache: ContextVar[ControlRepo | None] = ContextVar("aiflow_runtime_repo_cache", default=None)
    runtime_repo_cache_enabled: ContextVar[bool] = ContextVar("aiflow_runtime_repo_cache_enabled", default=False)
    capacity_history_lock = threading.Lock()
    capacity_history_cache: dict[str, Any] = {"at": 0.0, "samples": []}
    state_snapshot_lock = threading.Lock()
    state_snapshot_cache: dict[bool, tuple[float, dict[str, Any]]] = {}

    def capacity_history() -> list[dict[str, Any]]:
        """Share the completed-work estimate sample across concurrent worker polls for one second."""
        with capacity_history_lock:
            now = time.monotonic()
            if now - float(capacity_history_cache["at"]) >= 1.0:
                capacity_history_cache["samples"] = capacity.history(ctx.db)
                capacity_history_cache["at"] = now
            return capacity_history_cache["samples"]

    def cached_state_snapshot(include_signin: bool) -> dict[str, Any]:
        """Coalesce identical console snapshot reads while several views load at the same time."""
        requested_at = time.monotonic()
        with state_snapshot_lock:
            cached = state_snapshot_cache.get(include_signin)
            # A caller that began while the preceding snapshot was being built may share it.
            # Requests arriving after that read finished always see current database state.
            if cached and requested_at < cached[0]:
                return cached[1]
            from .state import snapshot
            data = snapshot(ctx, include_signin=include_signin)
            state_snapshot_cache[include_signin] = (time.monotonic(), data)
            return data

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
        if allowed_kinds is not None and current_review_item_kind(ticket) not in allowed_kinds:
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
        kind = review_item_kind(ticket, latest_dev)
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

    # --- health ---------------------------------------------------------------------------------------------
    @app.get("/v1/console/session")
    def console_session(request: Request) -> dict:
        user = (console_user(request, None) if ctx.settings.console_login else optional_console_user(request))
        if user is None:
            return {"authenticated": False}
        effective = user.view_as or user.role
        return {"authenticated": True, "user": user.subject, "role": effective,
                "role_label": ROLE_LABELS[effective], "roles": list(user.assigned_roles),
                "role_labels": [ROLE_LABELS[role] for role in user.assigned_roles],
                "permissions": sorted(user.permissions), "landing_page": landing_page((effective,)),
                "can_view_as": user.is_administrator, "viewing_as": user.view_as,
                "role_options": [{"id": role, "label": ROLE_LABELS[role],
                                  "permissions": sorted(ROLE_PERMISSIONS[role]),
                                  "landing_page": landing_page((role,))} for role in CANONICAL_ROLES]}

    def validate_project_keys(project_keys: list[str] | None) -> list[str] | None:
        if project_keys is None:
            return None
        selected = list(dict.fromkeys(str(key).strip() for key in project_keys if str(key).strip()))
        unknown = sorted(set(selected) - set(ctx.repo().projects))
        if unknown:
            raise HTTPException(400, "unknown project: " + ", ".join(unknown))
        return selected

    @app.get("/v1/console/users")
    def console_users(request: Request) -> dict:
        console_user(request, "console.manage")
        rows = ctx.db.all("SELECT email,display_name,roles,project_keys,disabled,last_login_at,created_at"
                          " FROM console_users ORDER BY lower(display_name),email")
        audits = ctx.db.all("SELECT ts,actor,action,accepted,result,payload FROM write_audit"
                            " WHERE target='console' AND action IN ('user_add','user_update','user_disable',"
                            "'user_enable','view_as','view_as_stop') ORDER BY id DESC LIMIT 100")
        return {"users": rows, "projects": sorted(ctx.repo().projects), "audit": audits,
                "role_options": [{"id": role, "label": ROLE_LABELS[role],
                                  "permissions": sorted(ROLE_PERMISSIONS[role])} for role in CANONICAL_ROLES]}

    @app.post("/v1/console/users")
    def add_console_user(body: ConsoleUserCreateBody, request: Request) -> dict:
        user = console_user(request, "console.manage", action="user_add")
        from .login import generate_password, hash_password, normalize_email
        try:
            email = normalize_email(body.email)
            roles = normalize_roles(body.roles)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project_keys = validate_project_keys(body.project_keys)
        password = generate_password()
        try:
            with ctx.db.pool.connection() as connection:
                with connection.transaction():
                    licensing.lock_resource_admission(connection)
                    people = connection.execute(
                        "SELECT count(*) AS n FROM console_users WHERE disabled=false").fetchone()["n"]
                    require_capacity("users", int(people), connection=connection)
                    created = connection.execute(
                        "INSERT INTO console_users (email,display_name,role,roles,project_keys,password_hash)"
                        " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING RETURNING email",
                        (email, body.display_name.strip()[:200], roles[0], list(roles), project_keys,
                         hash_password(password))).fetchone()
                    if not created:
                        raise HTTPException(409, "a console user with that email already exists")
                    audit_in(connection, "console", "user_add", "", "", None, user.subject, True, "Person added",
                             {"email": email, "roles": list(roles), "project_keys": project_keys})
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("console user could not be added")
            raise HTTPException(500, "person could not be added") from exc
        audit_user(request, user.subject, "person_added", email, {"roles": list(roles), "project_keys": project_keys})
        return {"email": email, "temporary_password": password}

    @app.put("/v1/console/users/{email}")
    def update_console_user(email: str, body: ConsoleUserUpdateBody, request: Request) -> dict:
        user = console_user(request, "console.manage", target="console", action="user_update")
        from .login import normalize_email
        try:
            email = normalize_email(email)
            roles = normalize_roles(body.roles)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project_keys = validate_project_keys(body.project_keys)
        with ctx.db.pool.connection() as connection:
            connection.execute("LOCK TABLE console_users IN EXCLUSIVE MODE")
            current = connection.execute("SELECT roles,disabled FROM console_users WHERE email=%s FOR UPDATE",
                                         (email,)).fetchone()
            if not current:
                raise HTTPException(404, "console user not found")
            roles_before = normalize_roles(current["roles"])
            if not current["disabled"] and "administrator" in roles_before and "administrator" not in roles:
                others = connection.execute("SELECT count(*) AS n FROM console_users WHERE email<>%s AND disabled=false"
                                            " AND 'administrator'=ANY(roles)", (email,)).fetchone()["n"]
                if int(others) == 0:
                    raise HTTPException(409, "the last administrator cannot be removed")
            connection.execute("UPDATE console_users SET role=%s,roles=%s,project_keys=%s,updated_at=now() WHERE email=%s",
                               (roles[0], list(roles), project_keys, email))
            audit_in(connection, "console", "user_update", "", "", None, user.subject, True, "Person updated",
                     {"email": email, "roles": list(roles), "project_keys": project_keys})
        audit_user_roles(request, user.subject, email, roles_before, roles, project_keys)
        return {"updated": True}

    @app.patch("/v1/console/users/{email}/disabled")
    def set_console_user_disabled(email: str, body: ConsoleUserDisabledBody, request: Request) -> dict:
        user = console_user(request, "console.manage", target="console",
                            action="user_disable" if body.disabled else "user_enable")
        from .login import normalize_email
        try:
            email = normalize_email(email)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with ctx.db.pool.connection() as connection:
            connection.execute("LOCK TABLE console_users IN EXCLUSIVE MODE")
            current = connection.execute("SELECT roles,disabled FROM console_users WHERE email=%s FOR UPDATE",
                                         (email,)).fetchone()
            if not current:
                raise HTTPException(404, "console user not found")
            roles = normalize_roles(current["roles"])
            if body.disabled and not current["disabled"] and "administrator" in roles:
                others = connection.execute("SELECT count(*) AS n FROM console_users WHERE email<>%s AND disabled=false"
                                            " AND 'administrator'=ANY(roles)", (email,)).fetchone()["n"]
                if int(others) == 0:
                    raise HTTPException(409, "the last administrator cannot be disabled")
            connection.execute("UPDATE console_users SET disabled=%s,updated_at=now() WHERE email=%s",
                               (body.disabled, email))
            action = "user_disable" if body.disabled else "user_enable"
            audit_in(connection, "console", action, "", "", None, user.subject, True,
                     "Person disabled" if body.disabled else "Person enabled", {"email": email})
        audit_user(request, user.subject, "person_disabled" if body.disabled else "person_enabled", email, {"disabled": body.disabled})
        return {"disabled": body.disabled}

    @app.post("/v1/console/view-as")
    def console_view_as(body: ViewAsBody, request: Request) -> JSONResponse:
        user = console_user(request, "console.manage", target="console", action="view_as")
        try:
            role = normalize_role(body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        from .login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret, view_as=role, sid=user.sid)
        audit("console", "view_as", "", "", None, user.subject, True, "Administrator viewing as role",
              {"role": role})
        response = JSONResponse({"viewing_as": role, "role_label": ROLE_LABELS[role]})
        app.state.set_session_cookies(response, token, SESSION_TTL_S)  # keeps the session id and the CSRF token in step
        return response

    @app.post("/v1/console/view-as/stop")
    def console_stop_view_as(request: Request) -> JSONResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user or not user.is_administrator:
            raise HTTPException(403, "not available for your role")
        from .login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret, sid=user.sid)
        audit("console", "view_as_stop", "", "", None, user.subject, True, "Administrator returned to own role",
              {"previous_role": user.view_as or ""})
        response = JSONResponse({"viewing_as": None})
        app.state.set_session_cookies(response, token, SESSION_TTL_S)
        return response


    # --- per-person console layouts (layouts.py, docs/v4/console-layouts.md) ---------------------------------
    def layout_page(page: str) -> str:
        if page not in layouts.PAGES:
            raise HTTPException(404, "unknown console page")
        return page

    @app.get("/v1/console/layouts")
    def console_layouts(request: Request) -> dict:
        user = console_user(request, None)
        return layouts.overview(ctx.db, user.subject, user.role)

    @app.put("/v1/console/layouts/{page}")
    def console_layout_save(page: str, body: layouts.LayoutBody, request: Request) -> dict:
        # Layouts are personal preferences, like reads, and remain available if the licence expires.
        user = console_user(request, None)
        try:
            blocks = layouts.normalise(layout_page(page), [b.model_dump() for b in body.blocks])
        except layouts.LayoutError as exc:
            raise HTTPException(400, str(exc)) from None
        layouts.save(ctx.db, user.subject, page, blocks)
        return layouts.page_view(page, user.role, blocks)

    @app.delete("/v1/console/layouts/{page}")
    def console_layout_reset(page: str, request: Request) -> dict:
        user = console_user(request, None)
        layouts.reset(ctx.db, user.subject, layout_page(page))
        return layouts.page_view(page, user.role, None)

    @app.get("/v1/console/preferences")
    def console_preferences(request: Request) -> dict:
        user = console_user(request, None)
        row = ctx.db.one("SELECT theme FROM console_preferences WHERE email=%s", (user.subject,))
        return {"theme": row["theme"] if row else "dark"}

    @app.put("/v1/console/preferences")
    async def console_preferences_save(request: Request) -> dict:
        user = console_user(request, None)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid console preferences") from None
        if not isinstance(body, dict) or body.get("theme") not in {"dark", "light"}:
            raise HTTPException(400, "theme must be dark or light")
        ctx.db.run("INSERT INTO console_preferences (email, theme) VALUES (%s, %s)"
                   " ON CONFLICT (email) DO UPDATE SET theme=EXCLUDED.theme, updated_at=now()",
                   (user.subject, body["theme"]))
        return {"theme": body["theme"]}

    def notification_admin(request: Request, action: str = "read") -> ConsoleUser:
        return console_user(request, {"notifications.manage"}, target="notifications", action=action)

    async def notification_json(request: Request) -> dict:
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid notification settings") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "invalid notification settings")
        return payload

    @app.get("/v1/notifications")
    def notification_settings(request: Request) -> dict:
        notification_admin(request)
        projects = [{"key": project.key, "name": project.key}
                    for project in sorted(ctx.repo().projects.values(), key=lambda item: item.key)]
        return notify.public_settings(ctx.db, projects)

    @app.put("/v1/notifications/channels")
    async def notification_channel_save(request: Request) -> dict:
        user = notification_admin(request, "write")
        payload = await notification_json(request)
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        if str(payload.get("kind") or "").strip().lower() == "telegram" and config.get("ba_chat_enabled"):
            project_key = str(config.get("project_key") or "").strip().upper()
            if project_key not in ctx.repo().projects:
                raise HTTPException(400, "choose a configured Jira project for BA chat")
        try:
            channel = notify.save_channel(ctx.db, payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit("notifications", "channel_save", "", "", None, user.subject, True,
              "notification channel saved", {"channel_id": channel["id"], "kind": channel["kind"],
                                               "enabled": channel["enabled"]})
        return {"channel": channel}

    @app.delete("/v1/notifications/channels/{channel_id}")
    def notification_channel_delete(channel_id: int, request: Request) -> dict:
        user = notification_admin(request, "write")
        if not notify.delete_channel(ctx.db, channel_id):
            raise HTTPException(404, "notification channel was not found")
        audit("notifications", "channel_delete", "", "", None, user.subject, True,
              "notification channel deleted", {"channel_id": channel_id})
        return {"deleted": True}

    @app.put("/v1/notifications/preferences")
    async def notification_preferences_save(request: Request) -> dict:
        user = notification_admin(request, "write")
        try:
            preferences = notify.save_preferences(ctx.db, await notification_json(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit("notifications", "preferences_save", "", "", None, user.subject, True,
              "notification routing preferences saved",
              {"route_count": sum(len(ids) for ids in preferences["routes"].values()),
               "quiet_hours_enabled": bool(preferences["quiet_start"] and preferences["quiet_end"])})
        return {"preferences": preferences}

    @app.post("/v1/notifications/channels/{channel_id}/test")
    def notification_channel_test(channel_id: int, request: Request) -> dict:
        notification_admin(request, "write")
        try:
            return notify.send_test(ctx, channel_id)
        except ValueError:
            raise HTTPException(404, "notification channel was not found") from None


    # --- licence and metered usage ---------------------------------------------------------------------------
    @app.get("/v1/licensing")
    def licensing_state(request: Request) -> dict:
        console_user(request, "licensing.manage")
        state = licensing.snapshot(ctx.db, ctx.settings.license_public_key)
        state["usage"] = licensing.resource_counts(ctx)
        state["sync_warnings"] = list(app.state.license_sync_warnings)
        return state

    @app.get("/v1/licensing/status")
    def licensing_status(request: Request) -> dict:
        console_user(request, "console.read")
        return licensing.status_fields(licensing.snapshot(ctx.db, ctx.settings.license_public_key))

    @app.post("/v1/licensing/activate")
    def activate_license(body: LicenseKeyBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            state = licensing.activate_online(ctx.db, body.key, ctx.settings.license_service_url,
                                              ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        audit("licensing", "license_activate", "", "", None, user.subject, True, "activated online",
              {"source": "online", "plan": state["plan"]})
        return state

    @app.post("/v1/licensing/offline")
    def activate_license_offline(body: OfflineLicenseBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            state = licensing.activate(ctx.db, body.license_file, ctx.settings.license_public_key, "offline")
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        audit("licensing", "license_activate", "", "", None, user.subject, True, "activated offline",
              {"source": "offline", "plan": state["plan"]})
        return state

    @app.put("/v1/licensing/limits")
    def update_license_limits(body: PlanLimitsBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_limits")
        try:
            state = licensing.save_limits(ctx.db, body.plan, body.limits, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        audit("licensing", "license_limits", "", "", None, user.subject, True, "limits updated",
              {"plan": body.plan, "limits": body.limits})
        return state

    @app.get("/v1/licensing/usage")
    def licensing_usage(request: Request) -> dict:
        console_user(request, "console.read")
        return {"rows": licensing.usage_report(ctx.db)}

    @app.get("/v1/licensing/usage.csv")
    def licensing_usage_csv(request: Request) -> PlainTextResponse:
        console_user(request, "console.read")
        content = licensing.usage_csv(licensing.usage_report(ctx.db))
        return PlainTextResponse(content, media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=ai-flow-usage.csv"})

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

    # --- work -----------------------------------------------------------------------------------------------
    def last_dev(key: str, done: bool = False) -> dict | None:
        """The latest finished dev run; done=True: the latest one that delivered work (a failed or blocked run
        in between does not answer the reviewer's findings)."""
        return ctx.db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
                          + (" AND outcome='done'" if done else "") + " ORDER BY started_at DESC LIMIT 1", (key,))

    def eligible(account_id: str, node: str, **options) -> list[dict]:
        """Tickets this account may take now (rules in features/work/eligible.py)."""
        return eligible_tickets(ctx, work_deps, account_id, node, **options)

    def route_capacity(account_id: str, node: str, offered: list[dict],
                       ticket_key: str | None = None) -> list[dict]:
        """Prefer an eligible account whose quota can cover the ticket's estimated upper bound.

        Candidate polling keeps its legacy behavior when no reliable quota forecast exists. If at least one eligible
        peer has a quota-safe forecast, only the best safe account sees that development ticket.
        """
        development = [ticket for ticket in offered if ticket.get("step") == "dev"]
        if not development:
            return offered
        # A partial index keeps the common no-forecast case cheap: candidate polling only reads
        # accounts whose saved forecast can actually influence routing.
        account_rows = ctx.db.all(
            "SELECT id,provider,tier,roles,quota_forecast FROM accounts WHERE quota_forecast->>'source'='reported'"
            " OR quota_forecast->>'confidence' IN ('medium','high') ORDER BY id")
        accounts_by_id = {str(row["id"]): row for row in account_rows}
        forecasts = {str(row["id"]): row.get("quota_forecast") or {} for row in account_rows}
        # Without reliable forecasts, quota routing cannot change the legacy offer. Avoid reading every account,
        # walking peer eligibility and loading historical capacity on each worker poll.
        if not account_rows:
            return offered
        if account_id not in accounts_by_id:
            current = ctx.db.one("SELECT id,provider,tier,roles,quota_forecast FROM accounts WHERE id=%s",
                                 (account_id,))
            if current:
                accounts_by_id[account_id] = current
                forecasts[account_id] = current.get("quota_forecast") or {}
        forecasts = capacity.with_active_quota_reservations(ctx.db, forecasts)
        samples = capacity_history()
        eligible_by_key: dict[tuple[str, str], dict[str, dict]] = {}
        for account in ctx.repo().accounts:
            if account.id not in accounts_by_id:
                continue
            machines = [node] if account.id == account_id else account.machines
            for machine in dict.fromkeys(machines):
                if account.id != account_id and (machine not in shared_accounts.online_nodes(ctx.db)
                                                 or not rollout.node_compatible(ctx, machine, RELEASES)["compatible"]):
                    continue
                try:
                    peer_offers = offered if account.id == account_id and machine == node else eligible(
                        account.id, machine, routing=False, capacity_routing=False, ticket_key=ticket_key)
                except HTTPException:
                    continue
                for item in peer_offers:
                    if item.get("step") == "dev":
                        eligible_by_key.setdefault((item["key"], item["step"]), {})[account.id] = {
                            **accounts_by_id[account.id], "tier": account.tier}
        if not eligible_by_key:
            return offered
        selected = capacity_routing_service.select_accounts(development, eligible_by_key, samples, forecasts,
                                                            ctx.provider_eligibility.state(persist=False))
        return [ticket for ticket in offered
                if ticket.get("step") != "dev" or (ticket["key"], ticket["step"]) not in selected
                or account_id in selected[(ticket["key"], ticket["step"])]]

    # What the eligibility rules of features/work/eligible.py need from this app; read when a rule runs, so tests may patch RELEASES.
    work_deps = SimpleNamespace(
        eligible=eligible, account_master_paused=account_master_paused, last_dev=last_dev,
        quota_forecast_at_risk=quota_forecast_at_risk, quota_peer_accounts=quota_peer_accounts,
        route_capacity=route_capacity, ticket_epic=lambda t: projects_routes.ticket_epic(t), releases=lambda: RELEASES)

    def master_ready(account_id: str, node: str) -> bool:
        if ctx.leases.cooling(account_id) or account_master_paused(account_id):
            return False
        ready = ctx.db.one("SELECT a.status, n.status AS node_status FROM accounts a, nodes n"
                           " WHERE a.id=%s AND n.name=%s", (account_id, node))
        return bool(ready) and ready["status"] in ("idle", "working", "probing") and ready["node_status"] == "online"

    def master_candidate(account_id: str, node: str) -> dict | None:
        repo = ctx.repo()
        account = repo.account(account_id)
        if not account or not account.runs_on(node):
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if account.paused or "master" not in (account.roles or []) or not master_ready(account_id, node):
            return None
        if account_quota_at_risk(account_id, {"master"}) or (account_id, "master") in role_health.disabled_roles(ctx.db):
            return None
        if not shared_accounts.may_take(ctx.db, account, node)[0]:
            return None
        if not master_budget_state()["enabled"]:
            return None
        held = set(ctx.leases.held_keys())
        if MASTER_SCOPE in held:
            return None
        recent = ctx.db.one("SELECT id, fence, ticket_key, provider, status, outcome, ended_at FROM runs"
                            " WHERE step='master' ORDER BY started_at DESC LIMIT 1")
        failover = False
        if recent:
            lease_lost = (recent["status"] == "running" and
                          ctx.leases.master_holder() != f"{recent['id']}:{recent['fence']}")
            failed_over = recent["outcome"] in ("quota", "lost") or lease_lost
            # Prefer the other provider for one cycle after a failure, and only while it has a ready master
            # account. Otherwise any ready master account may take over: cooldown and node checks above
            # already exclude the account that failed, so the flow never waits on an unavailable provider.
            failed_at = recent["ended_at"] or utcnow()
            if (failed_over and recent["provider"] == account.provider
                    and failed_at > utcnow() - timedelta(seconds=MASTER_CYCLE_S)
                    and any(a.provider != account.provider and "master" in (a.roles or [])
                            and any(master_ready(a.id, m) for m in a.machines) for a in repo.accounts)):
                return None
            # A lost or quota-stopped holder is an account failure, not a failing cycle: no scope backoff.
            failover = failed_over
        backoff = ctx.db.all(BACKOFF_SQL, {"step": "master"})
        if not failover and any(row["ticket_key"] == MASTER_SCOPE for row in backoff):
            return None
        if recent and recent["ended_at"] and recent["ended_at"] > utcnow() - timedelta(seconds=MASTER_CYCLE_S):
            if recent["outcome"] == "done":
                return None
        return {"key": MASTER_SCOPE, "project": "", "status": "Master", "type": "Master", "priority": "Highest",
                "labels": [], "summary": "Master monitoring cycle",
                "description": "Review incidents and follow up on blocked or slow work.",
                "jira_created": None, "step": "master"}

    def entitled_candidates(account: str, node: str, worker_id: str, state: dict) -> tuple[list[dict], str]:
        """Filter offers by account, machine, and project plan limits without affecting active runs."""
        candidates = eligible(account, node)
        project_priorities = {key: int(getattr(project, "priority", 5) or 5)
                              for key, project in ctx.repo().projects.items()}
        allowed: list[dict] = []
        refused: list[str] = []
        for ticket in candidates:
            try:
                licensing.enforce_project_entitlement(ctx, ticket["project"],
                                                      public_key=ctx.settings.license_public_key, state=state)
            except licensing.LicenseError as exc:
                refused.append(str(exc))
            else:
                if triage_claim_service.filter_candidates(ctx, [ticket]):  # a dev ticket waits for a verified owner answer
                    allowed.append(ticket)
        allowed = worker_candidates.diversify_dev_candidates(allowed, account, worker_id,
                                                              project_priorities, PRIORITY)
        reason = refused[0] if candidates and not allowed and refused else ""
        return allowed, reason

    load_policy_api.register_worker_candidate_routes(app, ctx, node_auth, RELEASES, entitled_candidates,
                                                     master_candidate)


    def related_hits(t: dict) -> list[dict]:
        """Up to 3 past entries related to the ticket summary; memory is advice, so an error gives none."""
        try:
            return memory.related(ctx.db, t["project"], t["summary"], exclude_ticket=t["key"], limit=3)
        except Exception as e:  # noqa: BLE001
            log.warning("memory lookup for %s failed: %s", t["key"], e)
            return []

    @app.post("/v1/work/claim")
    def claim(body: ClaimBody, node: str = Depends(node_auth)) -> Any:
        work_deps.master_candidate, work_deps.capacity_history = master_candidate, capacity_history
        work_deps.related_hits, work_deps.bot_identity = related_hits, bot_identity
        return claim_work(ctx, work_deps, body, node)

    # --- runs -----------------------------------------------------------------------------------------------
    @app.post("/v1/runs/{run_id}/heartbeat")
    def run_heartbeat(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        return run_inbox_control.heartbeat(ctx, run_row(run_id), run_id, body, node)

    @app.post("/v1/runs/{run_id}/result")
    def run_result(run_id: str, body: ResultBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        if int(run["fence"]) != body.fence:
            raise HTTPException(409, "run fence is no longer active")
        still_owner = ctx.leases.valid(run["ticket_key"], run_id, body.fence)
        details = dict(body.details)
        details.pop("pr_url_source", None)  # provenance is set by the control API, never trusted from the worker
        details.pop("quota_reservation", None)  # quota reservations are set by the control API
        if body.pr_url:
            details["pr_url_source"] = "current"
        if still_owner and run["step"] == "verify" and body.outcome in ("merged", "human"):
            try:
                details["post_merge_criteria"] = environment_reports.record_post_merge_criteria(
                    ctx, run, details.get("post_merge_criteria") or [])
            except environment_reports.UnmappedPostMergeCriteria as exc:
                raise HTTPException(422, str(exc)) from exc
        run_log = details.pop("support_log_text", None)  # raw logs never reach the runs table or its backups
        details.pop("support_log_truncated", None)
        outcome, error_kind = body.outcome, body.error_kind
        if still_owner:
            outcome, error_kind, details = load_policy.result_after_load_failure(
                ctx, node, body, details, utcnow().isoformat())
            body.outcome = outcome
            body.error_kind = error_kind
        late_result = run_inbox_control.finish(ctx, run, run_id, body, details, node)
        account_update_allowed = not late_result and still_owner
        status_update_allowed = account_update_allowed and run_inbox_control.account_update_allowed(
            ctx, run_id, run["account_id"], still_owner)
        if late_result:
            outcome = "lost"
        elif isinstance(run_log, str) and run_log:
            try:
                support_bundle.store_run_log(ctx, run_id, run_log)
            except Exception as exc:  # noqa: BLE001 - a missing diagnostic log must not lose the run result
                log.warning("support log for run %s not stored: %s", run_id, type(exc).__name__)
        if body.quota:
            quota_refresh.record_run_result(ctx.db, run, body, update_account=account_update_allowed)
        elif account_update_allowed:
            current = ctx.db.one("SELECT quota FROM accounts WHERE id=%s", (run["account_id"],)) or {}
            refresh_quota_forecast(run["account_id"], run["provider"], current.get("quota") or {})
        if account_update_allowed and body.error_kind == "auth":
            mark_machine_auth_failure(run["account_id"], node, body.summary)
        if status_update_allowed:
            if body.error_kind == "quota":
                until = body.reset_at or (utcnow() + timedelta(minutes=30))
                seconds = max(60, int((until - utcnow()).total_seconds()))
                changed = ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s, updated_at=now(),"
                                     " cooldown_since=coalesce(cooldown_since,now()), idle_since=NULL WHERE id=%s"
                                     " AND NOT EXISTS (SELECT 1 FROM runs WHERE account_id=%s AND status='running')",
                                     ("cooldown", until, body.summary[:300], run["account_id"], run["account_id"]))
                if changed:
                    ctx.leases.cooldown(run["account_id"], seconds, "quota")
            elif body.error_kind != "auth":
                ctx.db.run("UPDATE accounts SET status='idle', last_error=CASE WHEN %s IN ('failed','timeout') THEN %s ELSE '' END,"
                           " updated_at=now(), idle_since=coalesce(idle_since,now()), cooldown_since=NULL"
                           " WHERE id=%s AND status='working' AND NOT EXISTS"
                           " (SELECT 1 FROM runs WHERE account_id=%s AND status='running')",
                           (body.outcome, body.summary[:300], run["account_id"], run["account_id"]))
        if not late_result and body.outcome == "tool_missing" and run["step"] == "dev":
            recorded = tooling.record_missing(ctx, run, [str(x) for x in body.details.get("missing_tools") or []],
                                              body.summary)
            details["missing_tools"] = recorded
            if not recorded:
                outcome = "failed"
                ctx.db.run("UPDATE runs SET outcome='failed',error_kind=coalesce(nullif(error_kind,''),%s),details=details || %s"
                           " WHERE id=%s",
                           ("missing_requirement_unaccepted", J({"missing_tools": [], "missing_requirement_unaccepted": True}),
                            run_id))
            else:
                ctx.db.run("UPDATE runs SET details=details || %s WHERE id=%s", (J({"missing_tools": recorded}), run_id))
        if not late_result and body.outcome == "environment_missing" and run["step"] == "dev":
            recorded = environment_reports.record_missing(
                ctx, run, [str(x) for x in body.details.get("missing_environment") or []], body.summary)
            ctx.db.run("UPDATE runs SET details=details || %s WHERE id=%s",
                       (J({"missing_environment": recorded}), run_id))
            if not recorded:  # a name that is not in the project configuration is not an environment item
                outcome = "failed"
                ctx.db.run("UPDATE runs SET outcome='failed', error_kind=coalesce(nullif(error_kind,''),"
                           "'missing_environment_unaccepted') WHERE id=%s", (run_id,))
        if not late_result:
            try:
                knowledge.capture_run(ctx.db, run_id, ctx.settings.jira_site)
            except Exception as exc:  # noqa: BLE001 - per-run capture cannot undo a completed worker run
                log.warning("knowledge capture for run %s failed: %s", run_id, type(exc).__name__)
        dur = int((utcnow() - run["started_at"]).total_seconds() * 1000)
        if ctx.jira and run["step"] == "dev" and not late_result and body.outcome not in ("lost",):
            try:  # developer time on the ticket (each dev run, rework included); the detailed timeline is in OpenObserve
                ctx.jira.add_worklog(run["ticket_key"], max(60, dur // 1000), run["started_at"],
                                     f"AI Flow {run['step']} by {run['account_id']} ({run['provider']}"
                                     f"{'/' + run['model'] if run['model'] else ''}): {body.outcome}."
                                     f" Run {run_id}, fence {body.fence}.")
            except Exception as e:  # noqa: BLE001
                log.warning("worklog for %s failed: %s", run["ticket_key"], e)
        run_finished(ctx, run_id=run_id, ticket_key=run["ticket_key"], project=run["project"], step=run["step"],
                     fence=body.fence, account=run["account_id"], provider=run["provider"], model=run["model"], node=node,
                     duration_ms=dur, outcome=outcome, reason=error_kind or body.summary[:200], usage_in=body.tokens_in,
                     usage_out=body.tokens_out, cost_usd=body.cost_usd, still_owner=still_owner, pr_url=body.pr_url)
        try:  # quality by role: this result may switch the role off or settle a probation ticket
            if not late_result:
                role_health.evaluate(ctx, run["account_id"])
                if run["step"] == "verify":  # a verdict also decides the dev run it judged
                    dev = last_dev(run["ticket_key"])
                    if dev and dev["account_id"] != run["account_id"]:
                        role_health.evaluate(ctx, dev["account_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("role health after run %s failed: %s", run_id, e)
        return {"ok": True, "still_owner": still_owner}

    @app.post("/v1/runs/{run_id}/release")
    def run_release(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        released = run_inbox_control.release(ctx, run, run_id, body, node)
        ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'", (run["account_id"],))
        ctx.events.emit("run.released", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="released" if released else "not_owner")
        return {"released": released}

    @app.get("/v1/master/context")
    def master_context(run_id: str, fence: int, node: str = Depends(node_auth)) -> dict:
        master_run(run_id, fence, node, "context")
        return master_snapshot()

    @app.post("/v1/master/flow-bugs")
    def master_flow_bugs(body: MasterFlowBugsBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, "flow-bug-scan")
        return {"reports": process_flow_bugs(run)}

    @app.post("/v1/master/plan-outcome")
    def master_plan_outcome(body: MasterPlanOutcomeBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, "plan-outcome", {"valid": body.valid})
        inserted = ctx.db.one("INSERT INTO master_plan_cycles (run_id, valid) VALUES (%s,%s)"
                              " ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
                              (body.run_id, body.valid))
        recorded = (ctx.db.one("SELECT valid FROM master_plan_cycles WHERE run_id=%s", (body.run_id,))
                    if not inserted else {"valid": body.valid})
        valid = bool(recorded["valid"])
        if inserted:
            ctx.events.emit("master.plan", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                            account=run["account_id"], node=node, outcome="valid" if valid else "invalid",
                            reason="strict-format retry completed" if not valid else "valid action plan")
        streak_row = ctx.db.one(
            "SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid AND recorded_at > "
            "coalesce((SELECT max(recorded_at) FROM master_plan_cycles WHERE valid), 'epoch'::timestamptz)") or {}
        hour_row = ctx.db.one("SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid"
                              " AND recorded_at >= now()-interval '1 hour'") or {}
        streak, last_hour = int(streak_row.get("n") or 0), int(hour_row.get("n") or 0)
        incident_open = False
        if valid:
            from .incidents import close_incident
            close_incident(ctx, "master_invalid_plan", "global", "master returned a valid action plan")
        elif streak >= 3 or last_hour >= 5:
            from .incidents import open_incident
            incident_open = open_incident(
                ctx, "master_invalid_plan", "global", "critical", "master repeatedly returned an invalid plan",
                f"Invalid plan cycles: {streak} in a row and {last_hour} in the last hour. "
                "The master retried once with a strict JSON reminder on each cycle.")
        return {"valid": valid, "invalid_streak": streak, "invalid_last_hour": last_hour,
                "incident_opened": incident_open}

    def autonomy_report(weeks: int = 8, scope: tuple[str, ...] | None = None) -> dict[str, Any]:
        now = utcnow()
        this_monday = now.date() - timedelta(days=now.weekday())
        first_monday = this_monday - timedelta(days=(weeks - 1) * 7)
        start = datetime.combine(first_monday, datetime.min.time(), tzinfo=timezone.utc)
        end = now
        trend = []
        for index in range(weeks):
            week = first_monday + timedelta(days=index * 7)
            trend.append({"week_start": week.isoformat(), "finished_without_person": 0,
                          "needed_person": 0, "repeated_failures": 0})
        def trend_index(value: datetime) -> int | None:
            date = value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
            offset = (date - first_monday).days // 7
            return offset if 0 <= offset < weeks else None

        # A person limited to some projects gets the report of those projects only.
        ticket_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))" if scope is not None else ""
        scope_params: tuple = (list(scope),) if scope is not None else ()
        finished = ctx.db.all(
            "SELECT DISTINCT t.key, t.summary, t.done_at FROM tickets t WHERE t.status='Done'"
            + (" AND t.project=ANY(%s)" if scope is not None else "") +
            " AND t.done_at >= %s AND t.done_at <= %s AND EXISTS (SELECT 1 FROM runs r"
            " WHERE r.ticket_key=t.key AND r.status<>'running' AND lower(r.outcome) IN ('done','pass','passed')"
            " AND r.started_at <= t.done_at AND NOT EXISTS (SELECT 1 FROM runs later"
            " WHERE later.ticket_key=t.key AND later.started_at > r.started_at AND later.started_at <= t.done_at))"
            " AND NOT EXISTS (SELECT 1 FROM write_audit w WHERE w.ticket=t.key AND w.action='transition'"
            " AND w.payload->>'source'='human-review' AND w.result='Done' AND w.ts <= t.done_at)", scope_params + (start, end))
        for item in finished:
            i = trend_index(item["done_at"])
            if i is not None:
                trend[i]["finished_without_person"] += 1
        needed = ctx.db.all("SELECT observation_key, ticket, reason, options, sent_at FROM master_escalations"
                            " WHERE sent_at >= %s AND sent_at <= %s" + ticket_filter + " ORDER BY sent_at DESC",
                            (start, end) + scope_params)
        for item in needed:
            i = trend_index(item["sent_at"])
            if i is not None:
                trend[i]["needed_person"] += 1
        repeated = ctx.db.all("SELECT ts, type FROM events WHERE type IN ('flow_bug.created','flow_bug.repeated')"
                              " AND ts >= %s AND ts <= %s" + ticket_filter, (start, end) + scope_params)
        for item in repeated:
            i = trend_index(item["ts"])
            if i is not None:
                trend[i]["repeated_failures"] += 1
        week_start = datetime.combine(this_monday, datetime.min.time(), tzinfo=timezone.utc)
        current_finished = [row for row in finished if row["done_at"] >= week_start]
        current_needed = [row for row in needed if row["sent_at"] >= week_start]
        current_repeated = ctx.db.all(
            "SELECT f.ticket_key, f.signature, f.repeat_count, f.updated_at, t.summary, t.status"
            " FROM flow_bug_tracker f LEFT JOIN tickets t ON t.key=f.ticket_key"
            " WHERE f.updated_at >= %s" + (" AND t.project=ANY(%s)" if scope is not None else "") +
            " ORDER BY f.updated_at DESC LIMIT 50", (week_start,) + scope_params)
        # Accounts are shared infrastructure, not project data.
        quota_accounts = ctx.db.all("SELECT id,provider,quota,quota_source,quota_observed_at FROM accounts ORDER BY id") \
            if scope is None else []
        quota_state = quota_reporting.assessments(ctx.db, quota_accounts, now)
        return {"week_start": this_monday.isoformat(),
                "finished_without_person": current_finished[:50], "needed_person": current_needed[:50],
                "repeated_failures": current_repeated, "trend": trend,
                "escalations": escalations.weekly_metrics(ctx.db, weeks),
                "quota_accounts": [{"id": a["id"], "provider": a["provider"],
                                    "quota_awareness": quota_state.get(a["id"], {})} for a in quota_accounts]}

    @app.get("/v1/master/autonomy")
    def master_autonomy(request: Request) -> dict:
        user = console_user(request, "console.read")
        return {"budget": master_budget_state(), "report": autonomy_report(scope=user.project_keys)}

    @app.put("/v1/master/autonomy/budget")
    def master_budget_update(body: MasterBudgetBody, request: Request) -> dict:
        user = console_user(request, "console.write", target="master", action="daily-budget-update")
        ctx.db.run("INSERT INTO master_settings (singleton,daily_budget_minutes,updated_by) VALUES (true,%s,%s)"
                   " ON CONFLICT (singleton) DO UPDATE SET daily_budget_minutes=EXCLUDED.daily_budget_minutes,"
                   " updated_by=EXCLUDED.updated_by,updated_at=now()",
                   (body.daily_budget_minutes, user.subject))
        audit("master", "daily-budget-update", "", "", None, user.subject, True,
              f"daily budget set to {body.daily_budget_minutes} minutes",
              {"daily_budget_minutes": body.daily_budget_minutes})
        return {"budget": master_budget_state()}

    @app.get("/v1/master/report")
    def master_report(request: Request, weeks: int = Query(8, ge=1, le=52)) -> dict:
        user = console_user(request, "console.read")
        return autonomy_report(weeks, scope=user.project_keys)

    @app.post("/v1/master/actions")
    def master_action(body: MasterActionBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, body.action,
                         {"ticket": body.ticket, "intent_chars": len(body.intent), "action": body.action})
        ticket = body.ticket.strip()
        ticket_row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket,)) if ticket else None
        policy_project = ticket_row["project"] if ticket_row else None
        policy = ctx.repo().policy_for(policy_project)
        if not body.intent.strip():
            reason = "master action must include the request or observation intent"
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], run_id=body.run_id,
                            fence=body.fence, account=run["account_id"], node=node, outcome="denied", reason=reason,
                            action=body.action)
            raise HTTPException(400, reason)
        # Only the request is matched: what/why/result/detail describe observations (an offline node, a run
        # past its hard limit) and must not turn an allowed follow-up into an owner escalation.
        decision = decide_master_action(policy, body.action, body.intent, body.category)
        ctx.events.emit("policy.decision", ticket=ticket or run["ticket_key"], step="master", run_id=body.run_id,
                        fence=body.fence, account=run["account_id"], node=node, outcome=decision.decision,
                        reason=decision.reason[:200], action=body.action)

        def master_event(outcome: str, reason: str) -> None:
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], project=policy_project or "",
                            step="master", run_id=body.run_id, fence=body.fence, account=run["account_id"],
                            provider=run["provider"], model=run["model"], node=node, outcome=outcome,
                            reason=reason[:300], action=body.action)

        def refuse_master_action(reason: str, status: int = 409) -> None:
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason,
                  {"intent": body.intent[:1000], "why": body.why[:1000]})
            master_event("denied", reason)
            raise HTTPException(status, reason)

        if decision.decision == "escalate":
            options = [str(option).strip()[:500] for option in body.options if str(option).strip()][:5]
            if body.action == "escalate_to_owner" and (not body.why.strip() or not options):
                refuse_master_action("escalate_to_owner requires a reason and available options", 400)
            if not options:
                options = ["Keep the current state and wait for the next health check.",
                           "Choose another healthy account or retry after the reported reset time.",
                           "Ask an owner to investigate and decide the next step."]
            why = (body.why or f"The policy requires a person to decide: {decision.reason}").strip()[:1500]
            observation_key = body.observation_key.strip()[:500]
            if not observation_key:
                if body.kind and body.subject:
                    observation_key = f"incident:{body.kind}:{body.subject}"
                elif ticket:
                    updated = ctx.db.one("SELECT jira_updated FROM tickets WHERE key=%s", (ticket,))
                    observation_key = f"ticket:{ticket}:{updated['jira_updated'].isoformat() if updated and updated['jira_updated'] else 'current'}"
                else:
                    observation_key = f"master:{body.action}:{body.intent[:200]}"
            configured_channels = notify.configured_channels(ctx, "ticket_needs_person")
            created_row = ctx.db.one(
                "INSERT INTO master_escalations"
                " (observation_key,ticket,reason,options,run_id,pending_channels,delivered_channels,intent)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (observation_key) DO NOTHING RETURNING observation_key",
                (observation_key, ticket, why, J(options), body.run_id, configured_channels, [], body.intent[:1500]))
            escalation = ctx.db.one(
                "SELECT reason, options, pending_channels, delivered_channels, intent FROM master_escalations"
                " WHERE observation_key=%s", (observation_key,))
            # A NULL pending list marks rows created before channel delivery tracking was installed. Retry all
            # currently configured channels for those rows; the prior version could not record partial failures.
            pending_channels = set(escalation.get("pending_channels") or [])
            delivered_channels = set(escalation.get("delivered_channels") or [])
            if escalation.get("pending_channels") is None:
                pending_channels.update(set(configured_channels) - delivered_channels)
            elif not pending_channels and not delivered_channels:
                # An observation first seen with no alert channels can be delivered if channels are configured
                # later. Successfully delivered observations have a non-empty delivered list and stay suppressed.
                pending_channels.update(configured_channels)
            else:
                pending_channels.update(set(configured_channels) - delivered_channels)
            pending_before = sorted(pending_channels)
            if pending_before != sorted(escalation.get("pending_channels") or []):
                ctx.db.run("UPDATE master_escalations SET pending_channels=%s WHERE observation_key=%s",
                           (pending_before, observation_key))
            link = (ctx.settings.jira_site.rstrip("/") + f"/browse/{ticket}") if ticket and ctx.settings.jira_site else ""
            why = str(escalation["reason"])
            stored_options = [str(option) for option in (escalation.get("options") or [])]
            message = (f"Why a person is needed: {why}\nRequest: {escalation.get('intent') or body.intent[:1500]}\n"
                       "Available options:\n" + "\n".join(f"- {option}" for option in stored_options))
            attempt_channels = sorted(pending_channels.intersection(configured_channels))
            try:
                sent = (notify.send("warning", f"{ticket or 'AI Flow'} requires an owner decision", message, link,
                                    ctx=ctx, alert_kind="ticket_needs_person", project=policy_project or "AI Flow",
                                    ticket=ticket, needed=why, channels=attempt_channels, queued_ok=True)
                        if attempt_channels else [])
            except Exception as exc:  # Persist the pending channels and retry them on a later master cycle.
                log.warning("master escalation notification failed: %s", type(exc).__name__)
                sent = []
            sent = sorted(set(sent).intersection(attempt_channels))
            pending_channels.difference_update(sent)
            delivered_channels.update(sent)
            ctx.db.run("UPDATE master_escalations SET pending_channels=%s, delivered_channels=%s"
                       " WHERE observation_key=%s",
                       (sorted(pending_channels), sorted(delivered_channels), observation_key))
            already_sent = bool(not created_row and delivered_channels and not pending_channels and not sent)
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"],
                  body.action == "escalate_to_owner", decision.reason,
                  {"intent_chars": len(body.intent), "observation_key": observation_key, "options": options})
            audit("notify", "master-escalation", ticket, body.run_id, body.fence, run["account_id"], True,
                  ",".join(sent) if sent else ("pending: " + ",".join(sorted(pending_channels))
                                                if pending_channels else "no channel configured or duplicate"),
                  {"reason": why, "options": stored_options, "observation_key": observation_key,
                   "pending_channels": sorted(pending_channels)})
            master_event("escalated", why)
            return {"ok": True, "escalated": True, "reason": why, "options": stored_options,
                    "already_sent": already_sent, "sent": sent, "pending_channels": sorted(pending_channels)}

        if decision.decision != "allow":
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False,
                  decision.reason, {"intent_chars": len(body.intent)})
            master_event("denied", decision.reason)
            raise HTTPException(403, decision.reason)

        allowed_ticket_actions = {"retry", "move_to_todo", "restart_run"}
        ticket_row = None
        if body.action in allowed_ticket_actions:
            ticket_row = ctx.db.one("SELECT key, project, status FROM tickets WHERE key=%s", (ticket,)) if ticket else None
            if not ticket_row:
                reason = "a valid Jira ticket is required for this master action"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
                master_event("denied", reason)
                raise HTTPException(400, reason)
            if not ctx.jira:
                refuse_master_action("Jira is unavailable", 503)

        result = "ok"
        if body.action in ("retry", "move_to_todo"):
            if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
                refuse_master_action(f"a ticket in {ticket_row['status']} cannot be retried by the master")
            if body.action == "move_to_todo" and ticket_row["status"] not in ("To Do", "In Progress", "In Verify"):
                refuse_master_action("move_to_todo is limited to tickets in To Do, In Progress or In Verify")
            if ctx.leases.holder(ticket):
                refuse_master_action("ticket still has an active run; use restart_run after its work-time limit")
            if body.action == "retry":
                latest = ctx.db.one("SELECT outcome FROM runs WHERE ticket_key=%s AND status<>'running'"
                                    " ORDER BY started_at DESC LIMIT 1", (ticket,))
                if not latest or latest["outcome"] not in ("failed", "timeout", "lost", "quota", "blocked", "no_changes"):
                    refuse_master_action("retry requires a recent failed, blocked, quota or lost run")
            try:
                status = ctx.jira.transition_to(ticket, "To Do")
            except Exception as exc:  # noqa: BLE001 - record the refusal without exposing Jira internals
                refuse_master_action(f"Jira could not move {ticket} to To Do ({type(exc).__name__})", 502)
            ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, ticket))
            result = f"ticket moved to {status}"
        elif body.action == "pause_account":
            account_id = body.account_id.strip()
            account = ctx.db.one("SELECT id, status FROM accounts WHERE id=%s", (account_id,)) if account_id else None
            if not account:
                refuse_master_action("pause_account requires a configured account_id", 400)
            unhealthy = account["status"] in ("cooldown", "signed_out", "error") or bool(
                ctx.db.one("SELECT 1 AS issue FROM incidents WHERE closed_at IS NULL"
                           " AND kind IN ('account_quota','quota_high') AND (subject=%s OR subject LIKE %s)",
                           (account_id, account_id + ":%")))
            if not unhealthy:
                refuse_master_action("master may pause only an account with a quota or health issue")
            minutes = min(1440, max(1, int(body.pause_minutes)))
            ctx.db.run("INSERT INTO master_account_pauses (account_id,paused_until,reason,run_id)"
                       " VALUES (%s,now()+(%s * interval '1 minute'),%s,%s) ON CONFLICT (account_id) DO UPDATE SET"
                       " paused_until=EXCLUDED.paused_until,reason=EXCLUDED.reason,run_id=EXCLUDED.run_id,updated_at=now()",
                       (account_id, minutes, (body.why or body.intent)[:500], body.run_id))
            result = f"account paused for {minutes} minutes"
            ticket = ""
        elif body.action == "restart_run":
            if not body.target_run_id or not ctx.jira:
                refuse_master_action("restart_run requires target_run_id and an available Jira connection", 400)
            target = ctx.db.one("SELECT * FROM runs WHERE id::text=%s", (body.target_run_id,))
            if not target:
                refuse_master_action("target_run_id does not identify a recorded run", 404)
            if (target["ticket_key"] != ticket or target["step"] == "master" or target["status"] != "running"
                    or not ctx.leases.valid(target["ticket_key"], str(target["id"]), int(target["fence"]))):
                refuse_master_action("target run is not the active fenced run for this ticket")
            if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
                refuse_master_action(f"a run on a ticket in {ticket_row['status']} cannot be restarted by the master")
            project = ctx.repo().projects.get(target["project"])
            hard = project.work_minutes()["hard"] if project else work_limits.HARD
            elapsed = max(0.0, (utcnow() - target["started_at"]).total_seconds() / 60)
            if elapsed < hard:
                refuse_master_action(f"target run has not passed its {hard} minute work-time limit")
            if not ctx.leases.release(target["ticket_key"], str(target["id"]), int(target["fence"])):
                refuse_master_action("target run lease changed before restart")
            run_inbox_control.restart(ctx, target, body.target_run_id, body.run_id, body.why or body.intent)
            ctx.db.run("UPDATE accounts SET status='idle',updated_at=now() WHERE id=%s AND status='working'",
                       (target["account_id"],))
            try:
                status = ctx.jira.transition_to(ticket, "To Do")
            except Exception as exc:  # noqa: BLE001 - the old lease is already fenced off
                reason = f"run {body.target_run_id} was stopped, but Jira could not move {ticket} to To Do ({type(exc).__name__})"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], True, reason,
                      {"intent": body.intent[:1000], "why": body.why[:1000], "target_run_id": body.target_run_id})
                master_event("partial", reason)
                raise HTTPException(502, reason) from exc
            ctx.db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, ticket))
            result = f"stuck run {body.target_run_id} stopped; ticket moved to {status}"
            ticket_row["project"] = target["project"]
        elif body.action == "open_bug":
            candidates = {candidate["signature"] for candidate in flow_bug_candidates(ctx)
                          if candidate["creation_eligible"]}
            if not body.signature or body.signature not in candidates:
                refuse_master_action("open_bug requires an eligible repeated TESTFLOW failure signature")
            reports = process_flow_bugs(run)
            matching = next((report for report in reports if report.get("signature") == body.signature), None)
            if not matching:
                tracked = ctx.db.one("SELECT ticket_key FROM flow_bug_tracker WHERE signature=%s", (body.signature,))
                matching = {"action": "already_reported", "ticket": tracked["ticket_key"]} if tracked else None
            if not matching:
                refuse_master_action("the eligible flow failure could not be recorded; it will be retried next cycle", 502)
            ticket = matching.get("ticket", "")
            result = f"{matching['action']} {ticket}".strip()
        audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], True, str(result)[:500],
              {"intent": body.intent[:1000], "why": body.why[:1000], "account_id": body.account_id,
               "target_run_id": body.target_run_id, "signature": body.signature})
        master_event("completed", body.why or body.intent or str(result))
        return {"ok": True, "result": result, "escalated": False}

    # --- Jira writes (fenced) ---------------------------------------------------------------------------------


    # --- escalation classes and the owner decision register (docs/v4/escalations.md) --------------------------


    @app.get("/v1/github/ci-log")
    def github_ci_log(run_id: str, fence: int, branch: str = "", sha: str = "", lines: int = Query(200, ge=1, le=1000),
                      node: str = Depends(node_auth)) -> dict:
        """The tail of the failing CI job log of the run's pull request: a developer never has to ask for it."""
        run = fenced(run_id, fence, "github", "ci-log", {"branch": branch, "sha": sha[:12], "lines": lines})

        def refuse(reason: str) -> None:
            audit("github", "ci-log", run["ticket_key"], run_id, fence, run["account_id"], False, reason,
                  {"branch": branch, "sha": sha[:12]})
            raise HTTPException(403, reason)

        if run["node"] != node:
            refuse("CI logs are available only to the node running this developer run")
        if run["step"] != "dev":
            refuse("CI logs are available only to a developer run")
        project = ctx.repo().projects[run["project"]]
        expected_branch = str(run.get("branch") or "")
        expected_pr = str(run.get("pr_url") or "")
        pr_match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?", expected_pr, re.I)
        expected_repository = repository_for_run(run, project)

        if not pr_match or not expected_branch or not expected_repository:
            refuse("developer run has no recorded pull request")
        pr_repository = f"{pr_match.group(1)}/{pr_match.group(2)}"
        if pr_repository.lower() != expected_repository.lower():
            refuse("recorded pull request does not match the run repository")
        if branch and branch != expected_branch:
            refuse("requested branch is not the run's pull request branch")
        if not branch and not sha:
            raise HTTPException(400, "give the recorded pull request branch or commit sha")
        github = github_gateway(ctx)
        if not github:
            raise HTTPException(503, "GitHub App is not connected")
        repository = expected_repository.split("/", 1)[-1]
        pull_request = github.pr(repository, int(pr_match.group(3)))
        head = pull_request.get("head") or {}
        head_repo = head.get("repo") or {}
        pull_request_sha = str(head.get("sha") or "")
        if (int(pull_request.get("number") or 0) != int(pr_match.group(3))
                or head.get("ref") != expected_branch
                or str(head_repo.get("full_name") or "").lower() != expected_repository.lower()
                or not pull_request_sha):
            refuse("recorded pull request no longer matches the run's repository and branch")
        if sha and sha.lower() != pull_request_sha.lower():
            refuse("requested commit is not the current head of the run's pull request")
        sha = sha or pull_request_sha
        log_tail = github.failing_job_log(repository, sha, lines)
        audit("github", "ci-log", run["ticket_key"], run_id, fence, run["account_id"], True,
              log_tail.get("job") or "no failing job", {"sha": sha[:12], "lines": lines})
        return {"sha": sha, **log_tail}


    # --- GitHub (fenced) --------------------------------------------------------------------------------------
    @app.post("/v1/github/token")
    def github_token(body: GitHubTokenBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "token", {"repo": body.repo})
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, body.repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        permissions = ({"contents": "read", "metadata": "read"}
                       if run["step"] in ("triage", "version_release", "post_merge") else
                       {"contents": "write", "metadata": "read", "workflows": "write"})
        if run["step"] == "version_release" and not version_release.enabled(project):
            raise HTTPException(403, "project is not enabled for version releases")
        github = github_gateway(ctx, repository)
        if getattr(github, "kind", "app") == "token":
            branch = body.branch.strip()
            if branch and (run["step"] != "dev" or not branch.startswith(f"{run['ticket_key']}-")
                           or not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                           or branch.startswith("/") or branch.endswith("/") or ".." in branch
                           or "@{" in branch):
                raise HTTPException(422, "the Git transfer branch is invalid for this run")
            expires = utcnow() + timedelta(hours=1)
            connection_id = github_connection_used.get()
            capability = _issue_git_proxy_token(ctx, {
                "connection": connection_id, "repo": repository.lower(), "run_id": body.run_id,
                "fence": body.fence, "node": node, "branch": branch,
                "expires": int(expires.timestamp()),
            })
            audit("github", "token", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
                  "repository Git relay capability issued",
                  {"repo": repository, "branch": branch, "expires": expires.isoformat(), "transport": "control"})
            return {"token": capability, "expires_at": expires.isoformat(),
                    "transport_url": f"{ctx.settings.control_url.rstrip('/')}/v1/github/git/{repository}.git"}
        token, expires = github.token(repository.split("/", 1)[-1], permissions)
        audit("github", "token", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "issued",
              {"repo": repository, "expires": expires.isoformat()})
        return {"token": token, "expires_at": expires.isoformat()}

    async def github_git_proxy(git_path: str, request: Request) -> Response:
        """Broker token-backed Git smart HTTP without exposing the stored access token to a worker."""
        capability = _verify_git_proxy_token(ctx, _git_proxy_credential(request))
        if not capability:
            raise HTTPException(401, "Git repository transfer credential is invalid or expired",
                                headers={"WWW-Authenticate": 'Basic realm="AI Flow Git transfer"'})
        repository, separator, suffix = git_path.partition(".git")
        if (not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or repository.lower() != str(capability.get("repo", ""))):
            raise HTTPException(403, "Git repository transfer is outside this credential's scope")
        suffix = ".git" + suffix
        if request.method == "GET" and suffix == ".git/info/refs":
            service = request.query_params.get("service", "")
            if service not in ("git-upload-pack", "git-receive-pack"):
                raise HTTPException(400, "unsupported Git service")
            operation = service
        elif request.method == "POST" and suffix in (".git/git-upload-pack", ".git/git-receive-pack"):
            operation = suffix.rsplit("/", 1)[-1]
        else:
            raise HTTPException(404, "unsupported Git repository transfer operation")

        purpose = str(capability.get("purpose") or "run")
        run = None
        run_id = ""
        fence = -1
        if purpose == "check_probe":
            if operation != "git-upload-pack":
                raise HTTPException(403, "check-probe Git credentials are read-only")
            node_name = str(capability.get("node") or "")
            project_key = str(capability.get("project") or "")
            project = fleet_routes.probe_project(node_name, project_key)
            if repository.lower() not in {name.lower() for name in project.configured_repositories}:
                raise HTTPException(409, "check-probe repository is no longer configured")
        elif purpose == "run":
            run_id = str(capability.get("run_id") or "")
            run = run_row(run_id)
            fence = int(capability.get("fence", -1))
            if (run["node"] != capability.get("node") or int(run["fence"]) != fence
                    or not ctx.leases.valid(run["ticket_key"], run_id, fence)
                    or not matches_run_repository(repository_for_run(run, ctx.repo().projects.get(run["project"])),
                                                  repository)):
                raise HTTPException(409, "Git repository transfer run is no longer active")
            if operation == "git-receive-pack" and not capability.get("branch"):
                raise HTTPException(403, "Git push is not allowed by this read-only credential")
        else:
            raise HTTPException(403, "Git repository transfer credential has an unsupported purpose")
        body_bytes = await request.body() if request.method == "POST" else b""
        branch = str(capability.get("branch") or "")
        if request.method == "POST" and operation == "git-receive-pack":
            targets = _receive_pack_target(body_bytes)
            if targets != {f"refs/heads/{branch}"}:
                raise HTTPException(403, "Git push is limited to the branch assigned to this run")

        connection_id = str(capability.get("connection") or "")
        gateway = github_gateway_for_connection(ctx, connection_id, repository)
        if getattr(gateway, "kind", "app") != "token":
            raise HTTPException(403, "Git relay credentials are only valid for access-token connections")
        upstream_token, _ = gateway.token(repository.split("/", 1)[-1])
        forwarded_headers = {key: value for key, value in request.headers.items()
                             if key.lower() in {"accept", "content-type", "git-protocol"}}
        try:
            import anyio
            upstream = await anyio.to_thread.run_sync(
                _github_git_upstream, repository, suffix, request.method,
                list(request.query_params.multi_items()), forwarded_headers, body_bytes, upstream_token)
        except httpx.HTTPError as exc:
            log.warning("GitHub repository transfer failed (%s)", type(exc).__name__)
            raise HTTPException(502, "GitHub repository transfer failed") from exc
        if request.method == "POST" and operation == "git-receive-pack":
            if run is None:
                raise HTTPException(403, "check-probe Git credentials are read-only")
            accepted = 200 <= upstream.status_code < 300
            audit("github", "git-push", run["ticket_key"], run_id, fence, run["account_id"], accepted,
                  "pushed" if accepted else f"GitHub returned {upstream.status_code}",
                  {"repo": repository, "branch": branch})
        response_headers = {key: value for key, value in upstream.headers.items()
                            if key.lower() in {"content-type", "cache-control", "expires", "pragma"}}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)

    @app.get("/v1/github/git/{git_path:path}", operation_id="githubGitProxyGet")
    async def github_git_proxy_get(git_path: str, request: Request) -> Response:
        return await github_git_proxy(git_path, request)

    @app.post("/v1/github/git/{git_path:path}", operation_id="githubGitProxyPost")
    async def github_git_proxy_post(git_path: str, request: Request) -> Response:
        return await github_git_proxy(git_path, request)

    @app.get("/v1/github/pr")
    def github_find_pr(run_id: str = Query(min_length=1), fence: int = Query(ge=1),
                       repo: str = Query(min_length=1, max_length=200),
                       head: str = Query(min_length=1, max_length=240), node: str = Depends(node_auth)) -> dict:
        run = fenced(run_id, fence, "github", "find_pr", {"repo": repo, "head": head})
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        pr = github_gateway(ctx, repository).find_pr(repository.split("/", 1)[-1], head)
        found = {"found": bool(pr)}
        if pr:
            found.update(url=pr.get("html_url", ""), number=pr.get("number"))
            head_sha = str((pr.get("head") or {}).get("sha") or "")
            if head_sha:
                found["head_sha"] = head_sha
        audit("github", "find_pr", run["ticket_key"], run_id, fence, run["account_id"], True,
              found.get("url", "not found"), {"repo": repository, "head": head})
        return found

    @app.post("/v1/github/pr")
    def github_pr(body: PRBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "pr", {"repo": body.repo, "head": body.head})
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, body.repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        github = github_gateway(ctx, repository)
        settings = settings_for_run(run, project)
        if body.base != settings["base"]:
            raise HTTPException(403, "pull request base does not match the repository settings")
        pr = github.create_pr(repository.split("/", 1)[-1], body.head, body.base, body.title, body.body, body.draft)
        ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s WHERE id=%s", (pr["html_url"], body.head, body.run_id))
        audit("github", "pr", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, pr["html_url"],
              {"number": pr.get("number"), "draft": body.draft, "repo": repository})
        ctx.events.emit("github.pr", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="opened", pr_url=pr["html_url"])
        return {"url": pr["html_url"], "number": pr.get("number")}

    @app.post("/v1/github/merge")
    def github_merge(body: MergeBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "merge", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run can merge")
        # ready pull requests merge one after another, each brought up to date first (docs/v4/merge-conflicts.md)
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        repository = repository_for_run(run, project)
        res = merge_queue.merge_next(ctx.db, github_gateway(ctx, repository), repository, body.number,
                                     run["ticket_key"], str(run["id"]),
                                     lambda: merge_command(run, body.number, body.title, body.method, node))
        if res.get("updated") or res.get("conflict"):
            ctx.events.emit("github.merge_queue", ticket=run["ticket_key"], run_id=str(run["id"]), fence=run["fence"],
                            account=run["account_id"], node=node,
                            outcome="updated" if res.get("updated") else "conflict", reason=res.get("message", "")[:200])
        if res.get("merged"):  # the verify step's merge: the commit and the time are kept on the ticket
            merge_flow.record_merge(ctx, run["ticket_key"], merge_flow.latest_pr(ctx.db, run["ticket_key"]),
                                    str(res.get("sha") or ""), "flow", settled=False)
        return res

    review_queue = review_queue_api.ReviewQueueAPI(
        ctx, audit, repository_for_run, github_gateway, lambda t: projects_routes.ticket_epic(t), log)
    review_item_kind = review_queue.item_kind
    current_review_item_kind = review_queue.current_item_kind
    review_thread = review_queue.thread
    review_data = review_queue.data
    app.state.review_queue = review_queue  # routers outside api.py reach the queue through the app

    @app.post("/v1/github/webhook", include_in_schema=False)
    async def github_webhook(request: Request) -> dict:
        """Optional: a pull request event makes the reconcile loop look at that pull request at once. Without a
        configured secret the route does not exist for callers; the one-minute loop works alone."""
        secret = ctx.settings.github_webhook_secret
        if not secret:
            raise HTTPException(404, "GitHub webhooks are not configured")
        body = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise HTTPException(401, "invalid webhook signature")
        if request.headers.get("X-GitHub-Event") != "pull_request":
            return {"reconciled": 0}
        try:
            payload = json.loads(body)
            target = (str(payload["repository"]["full_name"]), int(payload["pull_request"]["number"]))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "not a pull request event") from None
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(merge_flow.reconcile, ctx, target)
        await run_in_threadpool(merge_flow.merge_ready, ctx)
        return {"reconciled": result["checked"]}

    review_queue.mount_human_reviews(app, console_user, optional_console_user)

    @app.get("/v1/reviews/export.csv")
    def export_review_metrics(request: Request, project: str = Query(default="")) -> PlainTextResponse:
        user = (console_user(request, "view.human_review") if ctx.settings.console_login
                else optional_console_user(request))
        scope = user.project_keys if user and user.project_keys is not None else None
        if project and scope is not None and project not in scope:
            audit("console", "access_denied", "", "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "project": project,
                                                    "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")
        metrics = review_data(project, scope)["metrics"]
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("metric", "value", "project"))
        for name, value in (
            ("people_needed_this_week", metrics["people_needed_this_week"]),
            ("people_needed_eight_weeks_ago", metrics["people_needed_eight_weeks_ago"]),
            ("finished_with_no_person_pct", metrics["finished_with_no_person_pct"]),
            ("median_wait_minutes", metrics["median_wait_minutes"]),
            ("approved_first_time_pct", metrics["approved_first_time_pct"]),
            ("sent_back_or_rejected_pct", metrics["sent_back_or_rejected_pct"]),
        ):
            writer.writerow((name, "" if value is None else value, project or "all"))
        writer.writerow(())
        writer.writerow(("week", "reason", "count", "project"))
        for week in metrics["weekly"]:
            for reason, count in week["by_reason"].items():
                writer.writerow((week["week"], reason, count, project or "all"))
        return PlainTextResponse(output.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=human-review-metrics.csv"})

    @app.post("/v1/reviews/{key}/approve")
    def approve_review(key: str, _reviewer: str = Depends(reviewer_auth)) -> dict:
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if not number:
                raise HTTPException(409, "ticket has no pull request to approve")
            run = fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            ctx.db.run("UPDATE tickets SET approved_at=coalesce(approved_at, now()) WHERE key=%s", (key,))
            result = queued_merge(run, number, f"{key}: {ticket['summary']}",
                                   (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                # approved: the merge step of the flow takes over, so nobody has to press merge later
                note = (f"Human review approval could not merge PR #{number} yet: {result.get('message', 'merge refused')}."
                        " The flow keeps the ticket as Ready to merge and merges it as soon as the pull request can merge.")
                reviewer_comment(key, run_id, fence, number, note)
                audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                      result.get("message", "merge refused"), {"kind": "review_before_merge"})
                queued = ""
                try:
                    queued = reviewer_transition(key, run_id, fence, merge_flow.READY_TO_MERGE, "approved, merge pending")
                except Exception as e:  # noqa: BLE001 - the Jira workflow may lack the status; approval still queues it
                    log.info("%s stays in Human review until it merges: %s", key, e)
                finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "queued": True, "status": queued or ticket["status"],
                        "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review approved in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            reviewer_comment(key, run_id, fence, number, note)
            merge_flow.record_merge(ctx, key, merge_flow.latest_pr(ctx.db, key), result.get("sha", ""), "flow",
                                    settled=False)
            status = reviewer_transition(key, run_id, fence, "Done", "human review approved")
            record_reviewer_action(ticket, run_id, fence, "approve")
            finish_reviewer_run(key, run_id, fence, "merged", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except HTTPException:
            audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                  "review approval failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review approval failed for %s", key)
            audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                  "review approval failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise HTTPException(502, "could not complete the review approval") from e

    @app.post("/v1/reviews/{key}/rework")
    def request_review_rework(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        comment = body.comment.strip()
        if not comment:
            raise HTTPException(400, "a rework comment is required")
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            note = f"Human review requested rework:\n\n{comment}"
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, add=("rework",))
            status = reviewer_transition(key, run_id, fence, "To Do", "human review requested rework")
            record_reviewer_action(ticket, run_id, fence, "rework", comment)
            finish_reviewer_run(key, run_id, fence, "rework", comment)
            return {"status": status}
        except HTTPException:
            audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                  "review rework failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review rework failed for %s", key)
            audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                  "review rework failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise HTTPException(502, "could not return the ticket for rework") from e

    @app.post("/v1/reviews/{key}/reject")
    def reject_review(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(400, "a rejection reason is required")
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if not number:
                raise HTTPException(409, "ticket has no pull request to reject")
            note = f"Human review rejected this change.\n\nReason: {reason}"
            reviewer_comment(key, run_id, fence, number, note)
            run = fenced(run_id, fence, "github", "close", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            repository = repository_for_run(run, project)
            gateway = github_gateway(ctx, repository)
            # a rejection is final: reconcile must not send the rejected ticket back because its PR is closed
            ctx.db.run("UPDATE tickets SET pr_state='closed', pr_url=coalesce(nullif(%s,''),pr_url),"
                       " merge_note=%s, pr_checked_at=now() WHERE key=%s",
                       ((merge_flow.latest_pr(ctx.db, key) or {}).get("url", ""), f"rejected in human review: {reason}"[:500], key))
            closed = gateway.close_pr(repository.split("/", 1)[-1], number)
            audit("github", "close", key, run_id, fence, run["account_id"], True,
                  "pull request closed", {"number": number, "repo": repository})
            reviewer_labels(key, run_id, fence, add=("rejected",), remove=("require-human",))
            status = reviewer_transition(key, run_id, fence, "Done", "human review rejected")
            record_reviewer_action(ticket, run_id, fence, "reject", reason)
            finish_reviewer_run(key, run_id, fence, "rejected", reason)
            return {"status": status, "closed": bool(closed)}
        except HTTPException:
            audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                  "review rejection failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review rejection failed for %s", key)
            audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                  "review rejection failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise HTTPException(502, "could not reject the reviewed change") from e

    @app.post("/v1/reviews/{key}/ask-ai")
    def ask_review_ai(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        question = (body.question or body.comment).strip()
        if not question:
            raise HTTPException(400, "a question for the AI is required")
        ticket, run_id, fence, number = reviewer_run(key, ("Human review", "Needs owner", "To Do", "In Progress", "In Verify"))
        try:
            note = f"Human review asked the AI:\n\n{question}"
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = ticket["status"]
            if status != "Needs owner":
                status = reviewer_transition(key, run_id, fence, "Needs owner", "reviewer asked the AI")
            escalations.raise_reviewer_question(ctx.db, key, ticket["project"], run_id, question)
            record_reviewer_action(ticket, run_id, fence, "ask-ai", question)
            finish_reviewer_run(key, run_id, fence, "question", question)
            return {"status": status, "thread": review_thread(key)}
        except HTTPException:
            audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                  "AI question failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review AI question failed for %s", key)
            audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                  "AI question failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise HTTPException(502, "could not send the question to the AI") from e

    @app.post("/v1/reviews/{key}/answer")
    def answer_review_question(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        answer = (body.answer or body.comment).strip()
        if not answer:
            raise HTTPException(400, "an answer is required")
        ticket, run_id, fence, number = reviewer_run(
            key, ("Needs owner",), allowed_kinds=("question_from_ai", "decision_needed"))
        try:
            reviewer_comment(key, run_id, fence, number, f"Human answer to the AI:\n\n{answer}")
            reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = reviewer_transition(key, run_id, fence, "To Do", "human answered the AI question")
            record_reviewer_action(ticket, run_id, fence, "answer", answer)
            finish_reviewer_run(key, run_id, fence, "answered", answer)
            return {"status": status}
        except HTTPException:
            audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                  "human answer failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review answer failed for %s", key)
            audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                  "human answer failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise HTTPException(502, "could not record the answer") from e

    @app.post("/v1/reviews/{key}/skip-gate")
    def skip_review_gate(key: str, _reviewer: str = Depends(reviewer_auth)) -> dict:
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if "require-human" not in (ticket.get("labels") or []):
                raise HTTPException(409, "this ticket has no human review gate to skip")
            if not number:
                raise HTTPException(409, "ticket has no pull request to skip the gate for")
            run = fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            result = queued_merge(run, number, f"{key}: {ticket['summary']}",
                                   (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                note = f"Human review gate could not be skipped because PR #{number} did not merge: "
                note += result.get("message", "merge refused")
                reviewer_comment(key, run_id, fence, number, note)
                audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                      result.get("message", "merge refused"), {"kind": "review_before_merge"})
                finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review gate skipped in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, remove=("require-human",))
            merge_flow.record_merge(ctx, key, merge_flow.latest_pr(ctx.db, key), result.get("sha", ""), "flow",
                                    settled=False)
            status = reviewer_transition(key, run_id, fence, "Done", "human review gate skipped")
            record_reviewer_action(ticket, run_id, fence, "skip-gate")
            finish_reviewer_run(key, run_id, fence, "skipped_gate", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except HTTPException:
            audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                  "review gate skip failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review gate skip failed for %s", key)
            audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                  "review gate skip failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise HTTPException(502, "could not skip the human review gate") from e

    @app.post("/v1/github/update-branch")
    def github_update_branch(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "update-branch", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run updates a PR branch")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        res = github.update_branch(repository.split("/", 1)[-1], body.number)
        audit("github", "update-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], res["ok"],
              res["message"][:200], {"number": body.number, "repo": repository})
        return res

    @app.get("/v1/github/checks")
    def github_checks(run_id: str, fence: int, sha: str, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node or not ctx.leases.valid(run["ticket_key"], run_id, fence):
            raise HTTPException(409, "not the owner of this run")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        return {"checks": github.checks(repository.split("/", 1)[-1], sha)}

    @app.post("/v1/github/comment")
    def github_comment(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "comment", {"number": body.number})
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        outcome = pr_comments.post(github, repository.split("/", 1)[-1], body.number, body.kind, body.body)
        audit("github", "comment", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "ok",
              {"number": body.number, "repo": repository, "kind": body.kind, "outcome": outcome})
        return {"ok": True, "outcome": outcome}

    @app.post("/v1/github/delete-branch")
    def github_delete_branch(body: BranchCleanupBody, node: str = Depends(node_auth)) -> dict:
        """Delete the ticket's remote work branch through the connection assigned to its repository."""
        run = fenced(body.run_id, body.fence, "github", "delete-branch", {"branch": body.branch})
        if run["step"] not in ("dev", "verify"):
            raise HTTPException(403, "only a development or verify run cleans up a branch")
        branch = body.branch.strip()
        if (not branch.startswith(f"{run['ticket_key']}-") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                or branch.endswith("/") or ".." in branch):
            raise HTTPException(422, "the branch does not belong to this ticket")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        try:
            deleted = github.delete_branch(repository.split("/", 1)[-1], branch)
        except GitHubError as exc:
            audit("github", "delete-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], False,
                  "GitHub refused the branch deletion", {"branch": branch, "repo": repository})
            raise HTTPException(502, "GitHub refused the branch deletion") from exc
        audit("github", "delete-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
              "deleted" if deleted else "already gone", {"branch": branch, "repo": repository})
        return {"ok": True, "deleted": deleted}

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


    # --- state for dashboards ---------------------------------------------------------------------------------
    @app.get("/v1/projects/{key}/versions")
    def project_versions(key: str) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        versions = ctx.db.all(
            "SELECT v.id, v.name, v.released, v.release_date, count(t.key)::int AS ticket_count,"
            " count(t.key) FILTER (WHERE t.status='Done')::int AS done_count"
            " FROM versions v LEFT JOIN ticket_versions tv ON tv.version_id=v.id"
            " LEFT JOIN tickets t ON t.key=tv.ticket_key WHERE v.project=%s"
            " GROUP BY v.id, v.name, v.released, v.release_date ORDER BY v.name, v.id", (key,))
        for version in versions:
            version["open_tickets"] = ctx.db.all(
                "SELECT t.key, t.summary, t.status FROM ticket_versions tv"
                " JOIN tickets t ON t.key=tv.ticket_key"
                " WHERE tv.version_id=%s AND t.project=%s AND t.status <> 'Done' ORDER BY t.key",
                (version["id"], key))
        return {"project": key, "versions": versions}

    @app.get("/v1/projects/{key}/report")
    def project_report(key: str, epic: str | None = None, format: str = Query(default="json")):
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        if format not in ("json", "md", "csv", "html"):
            raise HTTPException(400, "format must be json, md, csv or html")
        try:
            report = build_report(ctx, key, epic)
        except ReportNotFound as e:
            raise HTTPException(404, str(e)) from e
        if format == "md":
            filename = f"{epic or key}-progress.md"
            return PlainTextResponse(render_markdown(report), media_type="text/markdown",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "csv":
            filename = f"{epic or key}-progress.csv"
            return PlainTextResponse(render_csv(report), media_type="text/csv; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "html":
            return HTMLResponse(render_html(report))
        return report

    @app.post("/v1/projects/{key}/close")
    def close_project(key: str, body: CloseProjectBody | None = None) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        result = project_lifecycle.close_project(ctx, key, body.actor if body else "owner")
        return {"project": key, "status": result["status"], "version": result["version"],
                "report": result.get("report"), "markdown": result.get("markdown"),
                "study_case_ref": result["study_case_ref"], "created": result["created"],
                "sent_channels": result["sent_channels"]}

    @app.get("/v1/projects/{key}/study-cases")
    def project_study_cases(key: str, limit: int = Query(50, ge=1, le=100)) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        cases = project_lifecycle.study_cases(ctx.db, key, limit)
        return {"project": key, "study_cases": cases}

    # --- Business view: change requests (same access as the console, no node token) -----------------------------
    def business_project(key: str) -> str:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        return key

    @app.get("/v1/business/{key}/change-requests")
    def business_change_requests(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "jira_site": ctx.settings.jira_site, "epics": business.epics(ctx.db, project),
                "change_requests": business.change_requests(ctx.db, project, ctx.settings.jira_site)}

    @app.get("/v1/business/{key}/release-readiness")
    def business_release_readiness(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "versions": business.release_readiness(ctx.db, project)}

    @app.get("/v1/business/{key}/release-progress")
    def business_release_progress(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "versions": business.release_progress_of(ctx.db, project)}

    @app.post("/v1/business/{key}/change-requests")
    def business_change_request(key: str, body: ChangeRequestBody) -> dict:
        project = business_project(key)
        try:
            title, description, epic = business.validate(ctx.db, project, body.title, body.description, body.epic)
        except business.Invalid as e:
            raise HTTPException(400, str(e)) from e
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        created = business.create(ctx, project, title, description, epic)
        audit("jira", "create", created["key"], "", None, business.ACTOR, True, created["status"],
              {"project": project, "type": "Story", "labels": [business.LABEL], "epic": epic,
               "chars": len(description)})
        ctx.events.emit("business.change_request", ticket=created["key"], project=project, outcome=created["status"],
                        reason=title[:200])
        return created

    # --- Stakeholder acceptance and UAT feedback --------------------------------------------------------------
    def stakeholder_project(key: str):
        project = ctx.repo().projects.get(key)
        if not project:
            raise HTTPException(404, f"unknown project {key}")
        return project

    def stakeholder_changes(project: str) -> list[dict[str, Any]]:
        rows = ctx.db.all(
            "SELECT DISTINCT ON (t.key) t.key, t.summary, t.type, t.status, t.parent_key, p.type AS parent_type,"
            " r.branch, r.head_sha, r.summary AS change_summary, r.details, r.started_at, r.ended_at"
            " FROM runs r JOIN tickets t ON t.key=r.ticket_key LEFT JOIN tickets p ON p.key=t.parent_key"
            " WHERE r.project=%s AND t.project=%s AND r.step='dev' AND r.status<>'running'"
            " AND r.outcome IN ('done','merged')"
            " ORDER BY t.key, r.started_at DESC", (project, project))
        rows.sort(key=lambda r: r["ended_at"] or r["started_at"], reverse=True)
        out = []
        for row in rows[:30]:
            details = row["details"] or {}
            epic = row["key"] if row["type"] == "Epic" else (
                row["parent_key"] if row["parent_type"] == "Epic" else "")
            release_commit = (details.get("release") or details.get("version") or row["head_sha"] or row["branch"] or "")
            out.append({"key": row["key"], "summary": row["summary"], "type": row["type"],
                        "status": row["status"], "epic": epic, "branch": row["branch"],
                        "commit": row["head_sha"], "release_commit": release_commit,
                        "change_summary": row["change_summary"],
                        "changed_at": (row["ended_at"] or row["started_at"]).isoformat()})
        return out

    @app.get("/v1/stakeholder/{key}")
    def stakeholder_view(key: str) -> dict:
        project = stakeholder_project(key)
        configured = project.urls or {}
        return {"project": key, "urls": {"develop": str(configured.get("develop") or ""),
                                          "production": str(configured.get("production") or "")},
                "changes": stakeholder_changes(key)}

    @app.post("/v1/stakeholder/{key}/verdict")
    def stakeholder_verdict(key: str, body: StakeholderVerdictBody) -> dict:
        project = stakeholder_project(key)
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        sender = body.sender.strip()
        if not sender:
            raise HTTPException(400, "sender is required")
        change = ctx.db.one("SELECT key, summary, type, parent_key FROM tickets WHERE key=%s AND project=%s",
                            (body.change_key, key))
        if not change:
            raise HTTPException(400, "change must belong to this project")
        target = body.target_key.strip() or body.change_key
        if target != body.change_key and target != change["parent_key"]:
            raise HTTPException(400, "acceptance target must be the related ticket or its epic")
        if target != body.change_key and not ctx.db.one(
                "SELECT key FROM tickets WHERE key=%s AND project=%s AND type='Epic'", (target, key)):
            raise HTTPException(400, "acceptance target must be the related epic")
        if body.verdict == "not_expected" and (not body.expected.strip() or not body.actual.strip()):
            raise HTTPException(400, "expected and actual behaviour are required for UAT feedback")

        screenshot = decode_screenshot(body.screenshot_data, body.screenshot_filename)

        page_url = str((project.urls or {}).get(body.environment) or "").strip()
        page_label = page_url or "chưa deploy"
        release_commit = body.release_commit.strip() or "chưa có dữ liệu"
        if body.verdict == "expected":
            comment = (f"Comment nghiệm thu\nMôi trường: {body.environment}\n"
                       f"Release hoặc commit: {release_commit}\nNgười gửi: {sender}")
            try:
                comment_id = ctx.jira.comment(target, comment)
            except Exception as exc:  # Jira adapters and HTTP clients can raise different transport exceptions.
                log.warning("Stakeholder acceptance comment failed for %s", target)
                raise HTTPException(502, "Jira could not record the acceptance") from exc
            audit("jira", "stakeholder_acceptance", target, "", None, sender, True, comment_id,
                  {"project": key, "change": body.change_key, "environment": body.environment})
            ctx.events.emit("stakeholder.acceptance", ticket=target, project=key, outcome="accepted",
                            account=sender, reason=body.environment)
            return {"ok": True, "verdict": body.verdict, "ticket": target, "comment_id": comment_id}

        description = (f"Related change: {change['key']} - {change['summary']}\n"
                       f"Page URL: {page_label}\nEnvironment: {body.environment}\n"
                       f"Release or commit: {release_commit}\n"
                       f"Expected behaviour (mong đợi):\n{body.expected.strip()}\n\n"
                       f"Actual behaviour (thực tế):\n{body.actual.strip()}\n\n"
                       f"Sender (người gửi): {sender}")
        summary = f"[UAT {body.environment}] {change['key']}: {body.actual.strip().splitlines()[0][:120]}"
        try:
            issue_key = ctx.jira.create_issue(key, summary, description, issuetype="Bug", labels=["uat-bug"])
            status = ctx.jira.transition_to(issue_key, "Intake")
            if status != "Intake":
                raise RuntimeError("new UAT issue did not reach Intake")
            if screenshot:
                ctx.jira.add_attachment(issue_key, screenshot[0], screenshot[1], screenshot[2])
        except Exception as exc:  # Never tell the stakeholder a partial Jira write succeeded.
            log.warning("Stakeholder UAT issue creation failed for project %s", key)
            raise HTTPException(502, "Jira could not record the UAT feedback") from exc
        ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                   " VALUES (%s,%s,'Intake','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                   " status='Intake', type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                   " description=EXCLUDED.description, synced_at=now()",
                   (issue_key, key, ["uat-bug"], summary, description))
        audit("jira", "stakeholder_bug", issue_key, "", None, sender, True, status,
              {"project": key, "change": body.change_key, "environment": body.environment,
               "screenshot": bool(screenshot)})
        ctx.events.emit("stakeholder.feedback", ticket=issue_key, project=key, outcome="created",
                        account=sender, reason=body.environment, data={"related": body.change_key})
        return {"ok": True, "verdict": body.verdict, "key": issue_key, "status": status}

    @app.get("/v1/state", responses={200: {"description": "Command Center, Delivery, and SA snapshot",
                                            "content": {"application/json": {"schema": STATE_RESPONSE_SCHEMA}}}})
    def state(request: Request, authorization: str = Header(default="")) -> dict:
        if authorization:  # the deploy scripts on the control machine read it with the owner token (login is on)
            owner_auth(request, authorization)
        # The login middleware authenticates console sessions and permits this legacy owner-token route;
        # resolving the cookie here keeps scoped session data filtered while allowing the owner token alone.
        user = optional_console_user(request)
        include_signin = bool(user and has_permission(user.effective_roles, "nodes.manage"))
        data = cached_state_snapshot(include_signin)
        all_tickets = data["tickets"]
        if user and user.project_keys is not None:
            data = copy.deepcopy(data)
            all_tickets = data["tickets"]
            visible = set(user.project_keys)
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
            data["totals"] = ctx.db.one(
                "SELECT count(*) FILTER (WHERE started_at > now()-interval '24 hours') AS runs_24h,"
                " coalesce(sum(cost_usd) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS cost_24h,"
                " coalesce(sum(tokens_in) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_in_24h,"
                " coalesce(sum(tokens_out) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_out_24h,"
                " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome='done') AS done_24h,"
                " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome = ANY(%s)) AS failed_24h"
                " FROM runs WHERE project=ANY(%s)",
                (["failed", "blocked", "timeout", "lost"], list(visible)))
            data["totals"] = {key: int(value) if key in {"runs_24h", "tokens_in_24h", "tokens_out_24h",
                                                               "done_24h", "failed_24h"} else float(value)
                              for key, value in data["totals"].items()}
            visible_nodes = {node["name"] for node in data["nodes"] if visible.intersection(node.get("projects") or [])}
            data["nodes"] = [node for node in data["nodes"] if node["name"] in visible_nodes]
            data["accounts"] = [account for account in data["accounts"]
                                if any(machine.get("node") in visible_nodes
                                       for machine in account.get("machines") or [])]
            data["incidents"] = [item for item in data["incidents"]
                                 if item.get("subject") in visible or item.get("subject") in visible_nodes]
            data["incidents_recent"] = [item for item in data["incidents_recent"]
                                         if item.get("subject") in visible or item.get("subject") in visible_nodes]
            data["sa"] = {}
            data["version_policy"] = {}
        if user and not has_permission(user.effective_roles, "view.machines_ai"):
            data["nodes"] = []
            data["accounts"] = []
            data["sa"] = {}
            data["role_health"] = {"roles": [], "settings": {}}
            data["version_policy"] = {}
            data["incidents"] = []
            data["incidents_recent"] = []
            data["events"] = [{key: event.get(key) for key in ("ts", "type", "ticket", "outcome", "reason")}
                               for event in data["events"]]
            for ticket in data["tickets"]:
                if ticket.get("run"):
                    ticket["run"] = {key: ticket["run"].get(key) for key in
                                     ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress")}
                if ticket.get("last_run"):
                    ticket["last_run"] = {key: ticket["last_run"].get(key) for key in
                                          ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                           "duration_s", "pr_url", "already_done", "ended_age_s")}
            data["active_runs"] = [{key: run[key] for key in ("id", "ticket", "project", "summary", "step",
                                                                  "elapsed_s", "heartbeat_age_s", "lease_ms", "progress")}
                                    for run in data["active_runs"]]
            data["recent_runs"] = [{key: run[key] for key in ("id", "ticket", "summary", "step", "outcome",
                                                                  "error_kind", "duration_s", "pr_url", "already_done",
                                                                  "ended_age_s")}
                                    for run in data["recent_runs"]]
            for ticket in data["tickets"]:
                if ticket.get("run"):
                    ticket["run"] = {key: ticket["run"].get(key) for key in
                                     ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress")}
                if ticket.get("last_run"):
                    ticket["last_run"] = {key: ticket["last_run"].get(key) for key in
                                          ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                           "duration_s", "pr_url", "already_done", "ended_age_s")}
        return data

    @app.get("/v1/deploy/state")
    def deploy_state(_deploy: str = Depends(deploy_auth)) -> dict:
        """The registry state for aiflow4-autodeploy; works with console sign-in on because it checks the token."""
        from .state import snapshot
        return snapshot(ctx)

    @app.get("/v1/deploy/ci-state")
    def deploy_ci_state(repo: str = Query(pattern=r"^[\w.-]+/[\w.-]+$"), sha: str = Query(pattern=r"^[0-9a-fA-F]{7,64}$"),
                        _deploy: str = Depends(deploy_auth)) -> dict:
        """CI state of a commit (pending|failed|ok|none) through the connection for its repository."""
        from .canary import ci_state
        gateway = github_gateway(ctx, repo)
        return {"repo": repo, "sha": sha, "state": ci_state(gateway, repo, sha)}

    # --- scale proof: budgets, runs attached to releases, tickets for findings (docs/v4/scale-proof.md) ---------
    @app.get("/v1/scale-proof")
    def scale_proof_summary() -> dict:
        return scale_proof.summary(ctx.db)

    @app.post("/v1/scale-proof/results")
    def scale_proof_results(body: ScaleProofBody, actor: str = Depends(owner_auth)) -> dict:
        """CI submits a run: it is attached to the release, judged against the budgets here (the submitted verdict
        is ignored) and every finding becomes a ticket."""
        report = body.report
        if not isinstance(report.get("measurements"), dict) or not report["measurements"]:
            raise HTTPException(422, "the report has no measurements")
        run_id = scale_proof.record(ctx, report, body.release, body.source)
        tickets = scale_proof.file_tickets(ctx, run_id)
        run = ctx.db.one("SELECT release, passed, report FROM scale_proof_runs WHERE id=%s", (run_id,))
        audit("aiflow-control", "scale-proof-result", "", "", None, actor, True,
              "passed" if run["passed"] else "failed", {"run_id": run_id, "release": run["release"]})
        return {"run_id": run_id, "release": run["release"], "passed": run["passed"], "blocked": not run["passed"],
                "failures": run["report"]["failures"], "tickets": tickets}


    # --- memory: past tickets, run summaries and verify findings ------------------------------------------------


    # --- learned knowledge: automatic capture, isolated context packs, and Knowledge screen data --------------
    @app.get("/v1/knowledge/items")
    def knowledge_items(request: Request, project: str = "", q: str = "", include_retired: bool = True) -> dict:
        console_user(request, "knowledge.read")
        return knowledge.dashboard(ctx.db, project, q, include_retired)

    @app.post("/v1/knowledge/items")
    def knowledge_item_save(body: KnowledgeItemBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_save")
        try:
            item = knowledge.save_item(ctx.db, project=body.project, scope=body.scope, epic=body.epic, kind=body.kind,
                                       title=body.title, content=body.content, actor=user.subject)
        except knowledge.Invalid as exc:
            raise HTTPException(422, str(exc)) from exc
        audit("knowledge", "item_save", str(item["id"]), "", None, user.subject, True, "saved",
              {"kind": body.kind, "project": body.project, "scope": body.scope})
        return knowledge._pack_item(item)

    @app.post("/v1/knowledge/items/{item_id}/{action}")
    def knowledge_item_action(item_id: int, action: str, body: KnowledgeItemActionBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_" + action,
                            node=str(item_id))
        try:
            item = knowledge.moderate_item(ctx.db, item_id, action=action, actor=user.subject,
                                           content=body.content, title=body.title,
                                           duplicate_id=body.duplicate_id, project=body.project,
                                           scope=body.scope or None, epic=body.epic)
        except knowledge.Invalid as exc:
            raise HTTPException(422 if "not found" not in str(exc) else 404, str(exc)) from exc
        audit("knowledge", "item_" + action, str(item_id), "", None, user.subject, True, "updated",
              {"item_id": item_id})
        return knowledge._pack_item(item)

    # --- Knowledge screen: versioned control-repo resources -------------------------------------------------
    def load_managed_resources() -> None:
        knowledge.import_local(ctx.db, ctx.repo().root)

    def submit_resource_change(kind: str, resource_id: str, title: str, content: str, archived: bool,
                               actor: str, action: str, expected_version: int | None = None) -> dict:
        try:
            kind, resource_id, title, content, actor = knowledge.validate(kind, resource_id, title, content, actor)
        except knowledge.Invalid as e:
            raise HTTPException(400, str(e)) from e
        github = github_control_gateway(ctx)
        load_managed_resources()
        current = knowledge.get_resource(ctx.db, kind, resource_id)
        if expected_version is not None and (not current or int(current["version"]) != expected_version):
            raise HTTPException(409, "resource changed since it was loaded; refresh and try again")
        path = knowledge.path_for(kind, resource_id)
        repo_name = Path(ctx.settings.control_repo).name
        payload = knowledge.file_content(kind, resource_id, title, content, archived)
        try:
            base = github.default_branch(repo_name)
            if kind == "policy":
                branch = f"aiflow/policy-{resource_id.lower()}-{secrets.token_hex(4)}"
                github.create_branch(repo_name, branch, base)
                sha = github.commit_file(repo_name, branch, path, payload,
                                             f"policy: propose {action} {resource_id}")
                pr = github.create_control_pr(repo_name, branch, base, f"Policy: {title}",
                                                  f"Policy {action} for `{resource_id}` proposed by {actor}.\n\n"
                                                  "Merge only after owner approval in the AI Flow Knowledge screen.")
                ctx.db.run("INSERT INTO managed_resource_proposals (resource_type,resource_id,title,content,archived,"
                           "base_title,base_content,base_archived,base_version,actor,action,branch,pr_number,pr_url)"
                           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (kind, resource_id, title, content, archived, current["title"] if current else "",
                            current["content"] if current else "", current["archived"] if current else False,
                            current["version"] if current else 0, actor, action, branch, int(pr["number"]),
                            str(pr.get("html_url") or "")))
                audit("aiflow-control", "policy-pr", "", "", None, actor, True, str(pr["html_url"]),
                      {"type": kind, "id": resource_id, "branch": branch, "pr": int(pr["number"]), "commit": sha})
                ctx.events.emit("resource.policy_proposed", project=resource_id if resource_id != "default" else "",
                                account=actor, outcome="pending", reason=f"PR #{pr['number']}",
                                data={"type": kind, "id": resource_id})
                return {"pending": True, "type": kind, "id": resource_id, "title": title,
                        "pr_number": int(pr["number"]), "pr_url": str(pr.get("html_url") or ""), "version":
                        int(current["version"]) if current else 0}

            sha = github.commit_file(repo_name, base, path, payload, f"{kind}: {action} {resource_id}")
        except HTTPException:
            raise
        except Exception as e:  # GitHub errors vary between App and transport implementations.
            log.warning("control repo write failed for %s/%s", kind, resource_id)
            raise HTTPException(502, "control repo could not save the resource") from e
        row = knowledge.save_current(ctx.db, kind=kind, resource_id=resource_id, title=title, content=content,
                                     archived=archived, actor=actor, action=action, path=path, commit_sha=sha)
        audit("aiflow-control", "resource-commit", "", "", None, actor, True, sha,
              {"type": kind, "id": resource_id, "version": row["version"], "path": path})
        ctx.events.emit("resource.changed", account=actor, outcome=action, reason=f"{kind}/{resource_id}",
                        data={"type": kind, "id": resource_id, "version": row["version"], "commit": sha})
        return row

    @app.get("/v1/knowledge/resources")
    def knowledge_resources(request: Request, include_archived: bool = True) -> dict:
        console_user(request, "knowledge.read")
        load_managed_resources()
        return {"resources": knowledge.list_resources(ctx.db, include_archived),
                "proposals": knowledge.proposals(ctx.db)}

    @app.get("/v1/knowledge/resources/{kind}/{resource_id}/history")
    def knowledge_history(kind: str, resource_id: str, request: Request) -> dict:
        console_user(request, "knowledge.read")
        load_managed_resources()
        if kind not in knowledge.KINDS:
            raise HTTPException(404, "unknown resource type")
        return {"type": kind, "id": resource_id, "history": knowledge.history(ctx.db, kind, resource_id),
                "proposals": knowledge.proposals(ctx.db, kind, resource_id)}

    @app.post("/v1/knowledge/resources")
    def knowledge_save(body: KnowledgeBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="aiflow-control", action="resource_save")
        if not body.id.strip():
            raise HTTPException(400, "resource id is required")
        action = "archive" if body.archived else "save"
        return submit_resource_change(body.type, body.id, body.title, body.content, body.archived, user.subject, action,
                                      body.version)

    @app.post("/v1/knowledge/resources/{kind}/{resource_id}/restore")
    def knowledge_restore(kind: str, resource_id: str, body: KnowledgeRestoreBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="aiflow-control", action="resource_restore",
                            node=resource_id)
        load_managed_resources()
        version = ctx.db.one("SELECT title,content,archived FROM managed_resource_history WHERE resource_type=%s"
                             " AND resource_id=%s AND version=%s", (kind, resource_id, body.version))
        if not version:
            raise HTTPException(404, "resource version not found")
        return submit_resource_change(kind, resource_id, version["title"], version["content"], version["archived"],
                                      user.subject, "restore")

    @app.post("/v1/knowledge/proposals/{proposal_id}/approve")
    def knowledge_approve(proposal_id: int, body: KnowledgeApprovalBody,
                          _owner: str = Depends(owner_auth)) -> dict:
        proposal = ctx.db.one("SELECT * FROM managed_resource_proposals WHERE id=%s", (proposal_id,))
        if not proposal:
            raise HTTPException(404, "policy proposal not found")
        if proposal["status"] != "pending":
            raise HTTPException(409, "policy proposal is no longer pending")
        github = github_control_gateway(ctx)
        repo_name = Path(ctx.settings.control_repo).name
        owner = _owner
        try:
            merged = github.merge_pr(repo_name, int(proposal["pr_number"]),
                                         f"Policy: {proposal['title']}", "squash")
        except Exception as e:  # Do not expose provider responses or credentials through the UI.
            log.warning("policy proposal merge failed for PR #%s", proposal["pr_number"])
            raise HTTPException(502, "policy proposal could not be merged") from e
        if not merged.get("merged"):
            return {"merged": False, "message": merged.get("message", "GitHub refused the merge")}
        resource = knowledge.save_current(
            ctx.db, kind="policy", resource_id=proposal["resource_id"], title=proposal["title"],
            content=proposal["content"], archived=proposal["archived"], actor=proposal["actor"],
            action=proposal["action"], path=knowledge.path_for("policy", proposal["resource_id"]),
            commit_sha=str(merged.get("sha") or ""), approved_by=owner)
        ctx.db.run("UPDATE managed_resource_proposals SET status='merged',merged_at=now(),approved_by=%s WHERE id=%s",
                   (owner, proposal_id))
        audit("aiflow-control", "policy-merge", "", "", None, owner, True, str(merged.get("sha") or ""),
              {"id": proposal["resource_id"], "pr": proposal["pr_number"], "version": resource["version"]})
        ctx.events.emit("resource.policy_merged", project=proposal["resource_id"] if proposal["resource_id"] != "default" else "",
                        account=owner, outcome="merged", reason=f"PR #{proposal['pr_number']}",
                        data={"type": "policy", "id": proposal["resource_id"], "version": resource["version"]})
        if ctx.refresh_repo:
            try:
                ctx.refresh_repo()
            except Exception:
                log.warning("control repo refresh after policy merge failed")
        return {"merged": True, "sha": merged.get("sha", ""), "resource": resource}

    # --- encrypted backups and disaster recovery ----------------------------------------------------------
    def backup_config(include_history: bool = True) -> dict:
        row = ctx.db.one("SELECT * FROM control_backup_config WHERE id=true")
        if not row:
            raise HTTPException(503, "backup settings are not initialized")
        result = {key: value for key, value in row.items() if key not in {"id", "updated_at"}}
        for key, value in list(result.items()):
            if hasattr(value, "isoformat"):
                result[key] = value.isoformat()
        result["key_downloaded"] = bool(row["key_downloaded_at"])
        result["key_available"] = Path(ctx.settings.backup_key_file).is_file()
        if include_history:
            try:
                result["backups"] = control_backups.list_backups(result)
                result["storage_error"] = ""
            except control_backups.BackupError:
                result["backups"] = []
                result["storage_error"] = "Backup destination could not be listed; check its settings and credentials."
        return result

    @app.get("/v1/backups")
    def backup_status(request: Request) -> dict:
        console_user(request, "backup.read")
        return backup_config()

    @app.put("/v1/backups/config")
    def backup_configure(body: BackupConfigBody, request: Request) -> dict:
        user = console_user(request, "backup.manage", target="backup", action="configure")
        if body.destination_kind in {"local", "mounted"}:
            requested = Path(body.destination_path.strip())
            if not requested.is_absolute() or requested == Path("/") or ".." in requested.parts:
                raise HTTPException(400, "backup folder must be an absolute folder path")
            destination = requested.resolve(strict=False)
            if body.destination_kind == "local":
                local_root = Path(ctx.settings.backup_path).resolve(strict=False)
                if destination != local_root and not destination.is_relative_to(local_root):
                    raise HTTPException(400, "local backups must be stored under the durable local backup folder")
            protected = [Path(ctx.settings.control_repo).resolve(strict=False),
                         Path(ctx.settings.control_secrets_dir).resolve(strict=False),
                         Path(ctx.settings.console_secrets_dir).resolve(strict=False),
                         Path(ctx.settings.github_app_dir).resolve(strict=False),
                         Path(ctx.settings.jira_secret_dir).resolve(strict=False)]
            if any(destination == root or destination.is_relative_to(root) or root.is_relative_to(destination)
                   for root in protected):
                raise HTTPException(400, "backup folder must be separate from the control repository and secrets")
            path_value = str(destination)
            endpoint, bucket, prefix, region = "", "", "", "us-east-1"
        else:
            from urllib.parse import urlsplit
            parsed = urlsplit(body.s3_endpoint.strip())
            if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not body.s3_bucket.strip()):
                raise HTTPException(400, "enter an HTTPS S3-compatible endpoint and bucket")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}", body.s3_bucket.strip()):
                raise HTTPException(400, "S3 bucket name is invalid")
            endpoint, bucket = body.s3_endpoint.strip().rstrip("/"), body.s3_bucket.strip()
            prefix = "/".join(part for part in body.s3_prefix.strip().split("/") if part and part not in {".", ".."})
            region = body.s3_region.strip() or "us-east-1"
            path_value = "/srv/aiflow/backups"
        ctx.db.run("UPDATE control_backup_config SET schedule=%s,destination_kind=%s,destination_path=%s,s3_endpoint=%s,"
                   "s3_bucket=%s,s3_prefix=%s,s3_region=%s,retention_days=%s,updated_at=now() WHERE id=true",
                   (body.schedule, body.destination_kind, path_value, endpoint, bucket, prefix, region,
                    body.retention_days))
        audit("backup", "configure", "", "", None, user.subject, True, "backup settings updated",
              {"schedule": body.schedule, "destination": body.destination_kind, "retention_days": body.retention_days})
        return backup_config()

    @app.post("/v1/backups/key")
    def backup_key_download(request: Request) -> StreamingResponse:
        user = console_user(request, "backup.manage", target="backup", action="download_key")
        from fastapi import HTTPException as ApiError
        key_path = Path(ctx.settings.backup_key_file)
        try:
            with ctx.db.pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute("SELECT key_downloaded_at FROM control_backup_config WHERE id=true FOR UPDATE").fetchone()
                    if not row:
                        raise ApiError(503, "backup settings are not initialized")
                    if row["key_downloaded_at"] is not None or key_path.exists():
                        raise ApiError(409, "the backup encryption key has already been downloaded")
                    key = control_backups.create_key_file(key_path)
                    connection.execute("UPDATE control_backup_config SET key_downloaded_at=now(),updated_at=now() WHERE id=true")
        except ApiError:
            raise
        except OSError as exc:
            raise HTTPException(503, "the backup encryption key could not be created") from exc
        audit("backup", "download_key", "", "", None, user.subject, True, "one-time backup key download")
        return StreamingResponse(iter([key]), media_type="application/octet-stream",
                                  headers={"Content-Disposition": 'attachment; filename="aiflow4-backup-key"',
                                           "Cache-Control": "no-store", "Pragma": "no-cache",
                                           "X-Content-Type-Options": "nosniff"})

    @app.post("/v1/backups/run")
    def backup_request(request: Request) -> dict:
        user = console_user(request, "backup.manage", target="backup", action="run_now")
        ctx.db.run("UPDATE control_backup_config SET backup_requested_at=now(),updated_at=now() WHERE id=true")
        audit("backup", "run_now", "", "", None, user.subject, True, "backup queued")
        return {"queued": True}

    jira_connect.mount(app, ctx); setup_wizard.install(app, ctx)
    runtime_settings.install(app, ctx, console_user, audit)
    app.state.control_api_auth = {"node_auth": node_auth, "console_user": console_user, "audit": audit,
                                  "mark_machine_auth_failure": mark_machine_auth_failure}
    from .login import install_login
    install_login(app, ctx)
    eligibility.install(ctx, RELEASES, eligible, quota_forecast_at_risk, quota_peer_accounts)
    system_check_api.register_routes(app, ctx, console_user, optional_console_user); dashboard_timeout_routes.install(app, ctx, console_user, optional_console_user)
    release_routes = release_module.install(app, ctx, SimpleNamespace(node_auth=node_auth, release_reader=release_reader))
    fleet_routes = fleet_module.install(app, ctx, SimpleNamespace(audit=audit, audit_in=audit_in, console_user=console_user, create_join_grant=create_join_grant, current_release=release_routes.current_release, fleet_release=release_routes.fleet_release, node_auth=node_auth, release_assets=release_routes.release_assets, require_capacity=require_capacity))
    projects_routes = projects_module.install(app, ctx, SimpleNamespace(audit=audit, console_user=console_user, fenced=fenced, node_auth=node_auth, optional_console_user=optional_console_user, require_capacity=require_capacity))
    version_release_module.install(app, ctx, SimpleNamespace(account_master_paused=account_master_paused, bot_identity=bot_identity, fenced=fenced, node_auth=node_auth, quota_peer_release_candidates=quota_peer_release_candidates))
    return app
