"""Gateway helpers shared by the route groups: join-token checks, GitHub connections and gateways, and the git proxy credential.

Moved out of the control app entry point (JAIF-503) without changes."""
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
import json
import httpx
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import yaml
from fastapi import HTTPException, Request
from ..core.services import normalize_services
from . import (state_schema)
from .route_deps import github_connection_used
from .github import GitHubApp
from .github_connect import GitHubSecretStore
from .github_registry import github_connections as _github_registry
from .github_connections import DEFAULT_ID, Connection, GitHubConnections, GitHubTokenApp
from .application_context import Ctx
from .features.work.constants import utcnow

log = logging.getLogger("aiflow4.api")


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
