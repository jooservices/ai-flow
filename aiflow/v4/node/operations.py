"""Execute owner-requested node reconciliation using a small, fixed operation whitelist.

The node agent never accepts a shell command from the API. OS packages, git, Docker and PHP use the pinned
jooservices/server-installer CLI with argv built from a fixed map; declared packages run through a temporary
server-installer module that uses its package-manager abstraction. AI CLIs use fixed npm package names and exact
versions from versions.lock. Supporting tools use pinned official release artifacts and checksums. Services are
started through systemd with a validated unit name and checked locally before success is reported.
"""
from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx

AI_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "grok": "@vibe-kit/grok-cli",
    "opencode": "opencode-ai",
}
# jooservices/server-installer (TESTFLOW-68) is a bash CLI, pinned to a reviewed tag at a fixed root-owned path.
SERVER_INSTALLER_VERSION = "v1.1.0"
SERVER_INSTALLER = Path(f"/opt/server-installer/{SERVER_INSTALLER_VERSION}/bin/server-installer")
# Component -> server-installer module id. `git` and declared package lists use its packages module.
SERVER_INSTALLER_MODULES = {"packages": "packages", "git": "packages", "docker": "docker", "php": "php"}
SERVER_INSTALLER_BASE_PACKAGES = ("zip", "unzip")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}\Z")
_SAFE_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,127}\Z")
_SAFE_UNIT = re.compile(r"[A-Za-z0-9_.@-]{1,180}\.service\Z")
_SEMVER = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][A-Za-z0-9.-]+)?\Z")
_PYTHON_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?\Z")
_INSTALL_ORDER = ("packages", "git", "node", "uv", "python3", "docker", "php", "composer", "wp",
                  "gh", "claude", "codex", "codex-code-mode-host", "grok", "opencode")


class OperationError(RuntimeError):
    """A requested operation is unsupported or did not converge to the desired state."""


def _check_version(value: str, *, required: bool = False) -> str:
    version = str(value or "").strip()
    if required and not version:
        raise OperationError("AI CLI version is not pinned in versions.lock")
    if version and not _SAFE_VERSION.fullmatch(version):
        raise OperationError("version lock contains an invalid version")
    return version


def server_installer_command(name: str, version: str = "") -> tuple[list[str], dict[str, str]]:
    """The fixed argv and SI_* settings for a whitelisted OS component; no shell or request-supplied text."""
    module = SERVER_INSTALLER_MODULES.get(name)
    if module is None:
        raise OperationError(f"component is not in the server-installer whitelist: {name}")
    settings: dict[str, str] = {}
    if module == "php" and version:
        match = re.match(r"(\d+\.\d+)", _check_version(version))
        if not match:
            raise OperationError("PHP version in versions.lock must start with MAJOR.MINOR")
        settings["SI_PHP_VERSION"] = match.group(1)
    return [str(SERVER_INSTALLER), "apply", "--modules", module], settings


def _packages_module(packages: list[str]) -> str:
    """A temporary server-installer module using the installer's package-manager and module hooks."""
    if not packages or any(not isinstance(p, str) or not _SAFE_PACKAGE.fullmatch(p) for p in packages):
        raise OperationError("control repo packages must be a non-empty list of safe package names")
    names = tuple(dict.fromkeys((*SERVER_INSTALLER_BASE_PACKAGES, *packages)))
    quoted = " ".join(f'"{name}"' for name in names)
    return f'''#!/usr/bin/env bash
MODULE_ID="packages"
MODULE_TITLE="Control repo packages"
SI_PACKAGES_LIST=({quoted})

module_check() {{
  local pkg
  for pkg in "${{SI_PACKAGES_LIST[@]}}"; do
    si_pkg_is_installed "${{pkg}}" || return 1
  done
  return 0
}}

module_plan() {{
  local pkg
  for pkg in "${{SI_PACKAGES_LIST[@]}}"; do
    if si_pkg_is_installed "${{pkg}}"; then
      log_plan "${{pkg}}: already installed"
    else
      log_plan "${{pkg}}: will install via ${{SI_PKG_MANAGER}}"
    fi
  done
}}

module_apply() {{
  si_pkg_install "${{SI_PACKAGES_LIST[@]}}"
}}

module_verify() {{
  module_check
}}
'''


def _run_server_installer(name: str, version: str, packages: list[str] | None = None) -> None:
    command, settings = server_installer_command(name, version)
    if not SERVER_INSTALLER.is_file():
        raise OperationError(f"jooservices/server-installer {SERVER_INSTALLER_VERSION} is not installed at "
                             f"{SERVER_INSTALLER}")
    # Drop inherited SI_* values so only the pinned settings above reach the installer modules.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SI_")} | settings
    if name in {"packages", "git"}:
        module = _packages_module(packages or (["git"] if name == "git" else []))
        with tempfile.TemporaryDirectory(prefix="aiflow4-server-installer-") as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "server-installer").symlink_to(SERVER_INSTALLER)
            (root / "lib").symlink_to(SERVER_INSTALLER.parent.parent / "lib", target_is_directory=True)
            module_file = root / "modules" / "essentials" / "packages" / "module.sh"
            module_file.parent.mkdir(parents=True)
            module_file.write_text(module, encoding="utf-8")
            command[0] = str(root / "bin" / "server-installer")
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=1800, env=env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise OperationError(f"server-installer could not run for {name} ({type(exc).__name__})") from exc
    else:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationError(f"server-installer could not run for {name} ({type(exc).__name__})") from exc
    if result.returncode:
        raise OperationError(f"server-installer failed for {name} (exit {result.returncode})")


def _response(url: str, timeout: int = 120) -> httpx.Response:
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        return response
    except (httpx.HTTPError, OSError) as exc:
        raise OperationError(f"could not download a pinned tool artifact ({type(exc).__name__})") from exc


def _github_release_asset(repository: str, tag: str, asset_name: str) -> bytes:
    ref = f"tags/{tag}" if tag else "latest"
    response = _response(f"https://api.github.com/repos/{repository}/releases/{ref}", timeout=45)
    try:
        release = response.json()
    except ValueError as exc:
        raise OperationError("official release metadata is invalid") from exc
    asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
    digest = str((asset or {}).get("digest") or "")
    download_url = str((asset or {}).get("browser_download_url") or "")
    if not asset or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise OperationError(f"official release has no checksummed {asset_name} artifact")
    parsed = urlsplit(download_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise OperationError("official release artifact URL is invalid")
    payload = _response(download_url, timeout=900).content
    if hashlib.sha256(payload).hexdigest() != digest.removeprefix("sha256:"):
        raise OperationError(f"checksum verification failed for {asset_name}")
    return payload


def _archive_member(payload: bytes, suffix: str) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            matches = [member for member in archive.getmembers()
                       if member.isfile() and PurePosixPath(member.name).as_posix().endswith(suffix)]
            if len(matches) != 1:
                raise OperationError(f"official archive does not contain one {suffix} binary")
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise OperationError(f"official archive could not read {suffix}")
            return stream.read()
    except (tarfile.TarError, OSError) as exc:
        raise OperationError(f"official archive for {suffix} is invalid") from exc


def _install_file(name: str, payload: bytes, *, mode: int = 0o644,
                  directory: Path = Path("/usr/local/bin")) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def _install_binary(name: str, payload: bytes, *, directory: Path = Path("/usr/local/bin")) -> Path:
    return _install_file(name, payload, mode=0o755, directory=directory)


def _linux_arch() -> tuple[str, str]:
    machine = platform.machine().lower()
    mapping = {"x86_64": ("x86_64", "x64"), "amd64": ("x86_64", "x64"),
               "aarch64": ("aarch64", "arm64"), "arm64": ("aarch64", "arm64")}
    if not sys_platform_linux():
        raise OperationError("pinned binary installers currently support Linux nodes only")
    if machine not in mapping:
        raise OperationError(f"unsupported Linux architecture for pinned tool install: {machine}")
    return mapping[machine]


def sys_platform_linux() -> bool:
    return os.name == "posix" and platform.system() == "Linux"


def _normalized_version(version: str, tool: str, *, default: str = "") -> str:
    value = _check_version(version) or default
    if not value:
        return ""
    match = _SEMVER.fullmatch(value)
    if not match:
        raise OperationError(f"{tool} version in versions.lock must be MAJOR.MINOR.PATCH")
    return value.removeprefix("v")


def _install_uv(version: str) -> None:
    arch, _ = _linux_arch()
    normalized = _normalized_version(version, "uv")
    tag = normalized
    asset = f"uv-{arch}-unknown-linux-gnu.tar.gz"
    data = _github_release_asset("astral-sh/uv", tag, asset)
    for binary in ("uv", "uvx"):
        content = _archive_member(data, f"/{binary}")
        _install_binary(binary, content)


def _install_python(version: str) -> None:
    pinned = _check_version(version) or "3.12"
    if not _PYTHON_VERSION.fullmatch(pinned):
        raise OperationError("python3 version in versions.lock must be MAJOR.MINOR or MAJOR.MINOR.PATCH")
    pinned = pinned.removeprefix("v")
    if shutil.which("uv") is None:
        _install_uv("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    env["UV_PYTHON_BIN_DIR"] = "/usr/local/bin"
    try:
        result = subprocess.run(["uv", "python", "install", pinned, "--default", "--install-dir",
                                 "/usr/local/lib/aiflow4/uv-python"],
                                capture_output=True, text=True, timeout=1800, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError(f"python3 installer could not run ({type(exc).__name__})") from exc
    if result.returncode:
        raise OperationError(f"python3 installer failed (exit {result.returncode})")


def _install_node(version: str) -> None:
    _, node_arch = _linux_arch()
    normalized = _normalized_version(version, "node")
    if not normalized:
        index = _response("https://nodejs.org/dist/index.json", timeout=45).json()
        current = next((row for row in index if row.get("lts")), None)
        if not current:
            raise OperationError("Node.js has no published LTS release")
        normalized = str(current["version"]).removeprefix("v")
    asset_name = f"node-v{normalized}-linux-{node_arch}.tar.xz"
    sums = _response(f"https://nodejs.org/dist/v{normalized}/SHASUMS256.txt", timeout=45).text
    expected = next((line.split()[0] for line in sums.splitlines()
                     if len(line.split()) == 2 and line.split()[1] == asset_name), "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise OperationError(f"Node.js release has no checksum for {asset_name}")
    payload = _response(f"https://nodejs.org/dist/v{normalized}/{asset_name}", timeout=900).content
    if hashlib.sha256(payload).hexdigest() != expected:
        raise OperationError("checksum verification failed for Node.js")
    root = Path("/usr/local/lib/aiflow4/nodejs")
    root.mkdir(parents=True, exist_ok=True)
    version_dir = root / f"node-v{normalized}-linux-{node_arch}"
    try:
        with tempfile.TemporaryDirectory(prefix="node-", dir=root) as tmp:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
                archive.extractall(path=tmp, filter="data")
            unpacked = Path(tmp) / version_dir.name
            if not (unpacked / "bin" / "node").is_file():
                raise OperationError("Node.js archive is missing its node binary")
            if version_dir.exists():
                if version_dir.is_symlink() or not version_dir.is_dir():
                    raise OperationError("Node.js install target is not a regular directory")
                shutil.rmtree(version_dir)
            os.replace(unpacked, version_dir)
    except (tarfile.TarError, OSError) as exc:
        raise OperationError(f"Node.js archive could not be installed ({type(exc).__name__})") from exc
    for binary in ("node", "npm", "npx", "corepack"):
        source = version_dir / "bin" / binary
        if source.exists():
            link = Path("/usr/local/bin") / binary
            temp_link = link.with_name(f".{binary}.aiflow4")
            temp_link.unlink(missing_ok=True)
            temp_link.symlink_to(source)
            os.replace(temp_link, link)


def _install_gh(version: str) -> None:
    arch, _ = _linux_arch()
    normalized = _normalized_version(version, "gh")
    tag = f"v{normalized}" if normalized else ""
    # The latest endpoint's tag determines the archive's versioned folder and filename.
    if not normalized:
        response = _response("https://api.github.com/repos/cli/cli/releases/latest", timeout=45)
        try:
            tag = str(response.json()["tag_name"])
        except (ValueError, KeyError) as exc:
            raise OperationError("GitHub CLI release metadata is invalid") from exc
        normalized = _normalized_version(tag, "gh")
        tag = f"v{normalized}"
    asset = f"gh_{normalized}_linux_{'amd64' if arch == 'x86_64' else 'arm64'}.tar.gz"
    data = _github_release_asset("cli/cli", tag, asset)
    binary = _archive_member(data, "/bin/gh")
    _install_binary("gh", binary)


def _install_composer(version: str) -> None:
    reference = _normalized_version(version, "composer") or "latest-stable"
    base = f"https://getcomposer.org/download/{reference}/composer.phar"
    payload = _response(base, timeout=300).content
    digest_text = _response(base + ".sha256sum", timeout=45).text.strip()
    expected = digest_text.split()[0] if digest_text.split() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(payload).hexdigest() != expected:
        raise OperationError("Composer checksum verification failed")
    _install_binary("composer", payload)


def _install_wp(version: str) -> None:
    normalized = _normalized_version(version, "wp")
    tag = f"v{normalized}" if normalized else ""
    if not normalized:
        response = _response("https://api.github.com/repos/wp-cli/wp-cli/releases/latest", timeout=45)
        try:
            tag = str(response.json()["tag_name"])
        except (ValueError, KeyError) as exc:
            raise OperationError("WP-CLI release metadata is invalid") from exc
        normalized = _normalized_version(tag, "wp")
        tag = f"v{normalized}"
    asset = f"wp-cli-{normalized}.phar"
    payload = _github_release_asset("wp-cli/wp-cli", tag, asset)
    _install_binary("wp", payload)


def _install_codex_host(version: str) -> None:
    if not version:
        raise OperationError("codex-code-mode-host requires the pinned Codex version from versions.lock")
    arch, _ = _linux_arch()
    normalized = _normalized_version(version, "codex-code-mode-host")
    tag = f"rust-v{normalized}"
    asset = f"codex-code-mode-host-{arch}-unknown-linux-musl.tar.gz"
    payload = _github_release_asset("openai/codex", tag, asset)
    binary = _archive_member(payload, "/codex-code-mode-host")
    codex_path = shutil.which("codex")
    parent = Path(codex_path).resolve().parent if codex_path else Path("/usr/local/bin")
    _install_binary("codex-code-mode-host", binary, directory=parent)
    _install_file("codex-code-mode-host.version", f"{normalized}\n".encode(), directory=parent)


def _install_component(name: str, version: str, packages: list[str]) -> None:
    """Use server-installer for OS modules, fixed npm recipes for AI CLIs and pinned artifact recipes for tools."""
    if name == "packages":
        _run_server_installer(name, version, packages)
        return

    if name in SERVER_INSTALLER_MODULES:
        _run_server_installer(name, version)
        return

    if name in AI_PACKAGES:
        pinned = _check_version(version, required=True)
        command = ["npm", "install", "--global", "--no-audit", "--no-fund", "--registry",
                   "https://registry.npmjs.org", f"{AI_PACKAGES[name]}@{pinned}"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationError(f"{name} installer could not run ({type(exc).__name__})") from exc
        if result.returncode:
            raise OperationError(f"{name} installer failed (exit {result.returncode})")
        return

    if name == "uv":
        _install_uv(version)
        return
    if name == "python3":
        _install_python(version)
        return
    if name == "node":
        _install_node(version)
        return
    if name == "gh":
        _install_gh(version)
        return
    if name == "composer":
        _install_composer(version)
        return
    if name == "wp":
        _install_wp(version)
        return
    if name == "codex-code-mode-host":
        _install_codex_host(version)
        return

    raise OperationError(f"component is not in the install whitelist: {name}")


def _version_ok(actual: str, expected: str) -> bool:
    if actual in ("", "missing", "timeout"):
        return False
    return not expected or expected in actual


def _tool_version_ok(name: str, actual: str, expected: str) -> bool:
    if name == "codex-code-mode-host":
        if actual.startswith("codex-code-mode-host "):
            return _version_ok(actual, expected)
        return not expected and actual.startswith("present ") and actual != "present "
    return _version_ok(actual, expected)


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
    indexed = [(idx, item) for idx, item in enumerate(desired_tools) if isinstance(item, dict)]
    if len(indexed) != len(desired_tools):
        raise OperationError("tool plan is invalid")
    ordered = [item for _, item in sorted(indexed, key=lambda row: (
        _INSTALL_ORDER.index(str(row[1].get("name", ""))) if str(row[1].get("name", "")) in _INSTALL_ORDER
        else len(_INSTALL_ORDER), row[0]))]
    for item in ordered:
        name = str(item.get("name", ""))
        version = _check_version(str(item.get("version", "")), required=name in AI_PACKAGES)
        packages = item.get("packages") or []
        if not isinstance(packages, list):
            raise OperationError("package plan is invalid")
        if name != "packages" and _tool_version_ok(name, str(before.get(name, "missing")), version):
            continue
        _install_component(name, version, packages)
        installed.append(name)

    after = tool_versions(checked_tools)
    mismatches = [str(t.get("name")) for t in desired_tools if isinstance(t, dict)
                  and t.get("name") != "packages"
                  and not _tool_version_ok(str(t.get("name", "")),
                                           str(after.get(str(t.get("name", "")), "missing")),
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
