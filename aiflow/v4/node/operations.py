"""Execute owner-requested node reconciliation using a small, fixed operation whitelist.

The node agent never accepts a shell command from the API. OS packages, Docker and PHP are delegated to the
shared ``server_installer`` module; AI CLI installs use a fixed package map and exact versions from versions.lock.
Services are started through systemd with a validated unit name and checked locally before success is reported.
"""
from __future__ import annotations

import importlib
import re
import subprocess
from typing import Any

import httpx

AI_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "grok": "@vibe-kit/grok-cli",
    "opencode": "opencode-ai",
}
SYSTEM_COMPONENTS = {"packages", "docker", "php"}
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}\Z")
_SAFE_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,127}\Z")
_SAFE_UNIT = re.compile(r"[A-Za-z0-9_.@-]{1,180}\.service\Z")


class OperationError(RuntimeError):
    """A requested operation is unsupported or did not converge to the desired state."""


def _check_version(value: str, *, required: bool = False) -> str:
    version = str(value or "").strip()
    if required and not version:
        raise OperationError("AI CLI version is not pinned in versions.lock")
    if version and not _SAFE_VERSION.fullmatch(version):
        raise OperationError("version lock contains an invalid version")
    return version


def _server_installer():
    try:
        return importlib.import_module("server_installer")
    except ImportError as exc:
        raise OperationError("shared server-installer module is not installed on this node") from exc


def _install_component(name: str, version: str, packages: list[str]) -> None:
    """Use the shared installer for OS packages, Docker and PHP, and fixed package recipes for AI CLIs."""
    if name in SYSTEM_COMPONENTS:
        installer = _server_installer()
        if name == "packages":
            if not packages or any(not _SAFE_PACKAGE.fullmatch(p) for p in packages):
                raise OperationError("control repo packages must be a non-empty list of safe package names")
            installer.install_packages(packages=packages)
        elif name == "docker":
            installer.install_docker()
        else:
            installer.install_php(version=_check_version(version))
        return

    if name in AI_PACKAGES:
        pinned = _check_version(version, required=True)
        command = ["npm", "install", "--global", "--no-audit", "--no-fund", f"{AI_PACKAGES[name]}@{pinned}"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationError(f"{name} installer could not run ({type(exc).__name__})") from exc
        if result.returncode:
            raise OperationError(f"{name} installer failed (exit {result.returncode})")
        return

    if name == "codex-code-mode-host":
        installer = _server_installer()
        installer.install_codex_code_mode_host(version=_check_version(version, required=True))
        return

    if name in {"git", "uv", "gh", "node", "composer", "wp", "python3"}:
        installer = _server_installer()
        installer.install_tool(name=name, version=_check_version(version))
        return

    raise OperationError(f"component is not in the install whitelist: {name}")


def _version_ok(actual: str, expected: str) -> bool:
    if actual in ("", "missing", "timeout"):
        return False
    return not expected or expected in actual


def _service_health(service: dict[str, Any]) -> dict[str, Any]:
    unit = str(service.get("unit", ""))
    if not _SAFE_UNIT.fullmatch(unit):
        raise OperationError("control repo service unit is invalid")
    start = subprocess.run(["systemctl", "enable", "--now", unit], capture_output=True, timeout=120)
    if start.returncode:
        return {"name": str(service.get("name") or unit), "unit": unit, "healthy": False,
                "check": "systemd start failed"}
    active = subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True, timeout=30)
    if active.returncode:
        return {"name": str(service.get("name") or unit), "unit": unit, "healthy": False,
                "check": "systemd unit is not active"}
    url = str(service.get("healthcheck", "")).strip()
    if url:
        try:
            response = httpx.get(url, timeout=5, follow_redirects=False)
        except httpx.HTTPError:
            return {"name": str(service.get("name") or unit), "unit": unit, "healthy": False,
                    "check": "HTTP health check failed"}
        if not 200 <= response.status_code < 300:
            return {"name": str(service.get("name") or unit), "unit": unit, "healthy": False,
                    "check": f"HTTP health check returned {response.status_code}"}
    return {"name": str(service.get("name") or unit), "unit": unit, "healthy": True, "check": "green"}


def execute(operation: dict[str, Any]) -> dict[str, Any]:
    """Install missing components or reconcile all tools and services in a server-created operation payload."""
    payload = operation.get("payload") or {}
    from .agent import tool_versions

    action = payload.get("action")
    if action == "install":
        desired_tools = payload.get("tools") or []
        services: list[dict[str, Any]] = []
    elif action == "repair":
        desired_tools = payload.get("tools") or []
        services = payload.get("services") or []
    else:
        raise OperationError("operation is not in the install/repair whitelist")
    if not isinstance(desired_tools, list) or not isinstance(services, list):
        raise OperationError("operation payload is invalid")

    checked_tools = [str(t.get("name", "")) for t in desired_tools
                     if isinstance(t, dict) and t.get("name") != "packages"]
    before = tool_versions(checked_tools)
    installed: list[str] = []
    for item in desired_tools:
        if not isinstance(item, dict):
            raise OperationError("tool plan is invalid")
        name = str(item.get("name", ""))
        version = _check_version(str(item.get("version", "")), required=name in AI_PACKAGES)
        packages = item.get("packages") or []
        if not isinstance(packages, list):
            raise OperationError("package plan is invalid")
        if name != "packages" and _version_ok(str(before.get(name, "missing")), version):
            continue
        _install_component(name, version, packages)
        installed.append(name)

    after = tool_versions(checked_tools)
    mismatches = [str(t.get("name")) for t in desired_tools if isinstance(t, dict)
                  and t.get("name") != "packages"
                  and not _version_ok(str(after.get(str(t.get("name", "")), "missing")),
                                      str(t.get("version", "")))]
    service_results = [_service_health(s) for s in services if isinstance(s, dict)]
    if len(service_results) != len(services):
        raise OperationError("service plan is invalid")
    unhealthy = [s["name"] for s in service_results if not s["healthy"]]
    if mismatches:
        raise OperationError("desired tool versions are not healthy: " + ", ".join(mismatches))
    if unhealthy:
        raise OperationError("desired services are not healthy: " + ", ".join(unhealthy))
    return {"installed": installed, "tools": after, "services": service_results, "healthy": True}
