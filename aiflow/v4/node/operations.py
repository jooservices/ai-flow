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
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..core.toolchains import (CATALOG_TOOLS, NODE_TOOLS, PLAYWRIGHT_BROWSERS_DIR, STACK_TOOLS, locked_version,
                               pinned_version, toolchain_gaps,
                               tools_for_stacks_in_project, version_matches)

AI_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "grok": "@vibe-kit/grok-cli",
    "opencode": "opencode-ai",
}
# jooservices/server-installer (TESTFLOW-68) is a bash CLI, pinned to a reviewed tag at a fixed root-owned path.
SERVER_INSTALLER_VERSION = "v1.1.0"
SERVER_INSTALLER = Path(f"/opt/server-installer/{SERVER_INSTALLER_VERSION}/bin/server-installer")
# Shared with the control installer so both hosts verify the same pinned cosign artifacts.
COSIGN_VERSION = "v3.1.4"
COSIGN = {"x86_64": ("amd64", "424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa"),
          "aarch64": ("arm64", "ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2")}
# Component -> server-installer module id. `git` and declared package lists use its packages module.
SERVER_INSTALLER_MODULES = {"packages": "packages", "git": "packages", "docker": "docker", "php": "php"}
SERVER_INSTALLER_BASE_PACKAGES = ("zip", "unzip")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}\Z")
_SAFE_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,127}\Z")
_SAFE_UNIT = re.compile(r"[A-Za-z0-9_.@-]{1,180}\.service\Z")
_SEMVER = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][A-Za-z0-9.-]+)?\Z")
_REPORTED_VERSION = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){1,2}(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?(?:\+[A-Za-z0-9][A-Za-z0-9.-]*)?)(?![A-Za-z0-9])")
_PYTHON_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?\Z")
_INSTALL_ORDER = ("packages", "git", "node", "uv", "python3", "docker", "php", "composer", "wp", "podman",
                  "playwright", "chromium", "firefox", "webkit", "gh", "claude", "codex", "codex-code-mode-host",
                  "grok", "opencode", "gitleaks", "xdebug", "cosign")
RUNTIME_SECRET_FILE = Path("/etc/aiflow/secrets/runtime.env")
PROJECT_ENV_DIR = Path("/etc/aiflow/projects")


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


def _ensure_server_installer() -> None:
    """Provision the pinned TESTFLOW-68 installer on first use, without requiring git on a new node."""
    if SERVER_INSTALLER.is_file():
        return

    install_dir = SERVER_INSTALLER.parent.parent
    root = install_dir.parent
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{install_dir.name}-", dir=root) as tmp:
            staging = Path(tmp)
            extract_dir = staging / "extract"
            extract_dir.mkdir()
            payload = _response(
                f"https://api.github.com/repos/jooservices/server-installer/tarball/{SERVER_INSTALLER_VERSION}",
                timeout=900).content
            try:
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
                    archive.extractall(path=extract_dir, filter="data")
            except (tarfile.TarError, OSError) as exc:
                raise OperationError("pinned server-installer archive is invalid") from exc
            candidates = [path for path in extract_dir.rglob("bin/server-installer")
                          if path.is_file() and not path.is_symlink()]
            if len(candidates) != 1:
                raise OperationError("pinned server-installer archive is missing its CLI")
            source_dir = candidates[0].parent.parent
            staged_dir = staging / "install"
            os.replace(source_dir, staged_dir)
            binary = staged_dir / "bin" / "server-installer"
            os.chmod(binary, binary.stat().st_mode | 0o111)

            backup_dir = staging / "previous"
            if install_dir.exists():
                if install_dir.is_symlink() or not install_dir.is_dir():
                    raise OperationError("server-installer install path is not a regular directory")
                os.replace(install_dir, backup_dir)
            try:
                os.replace(staged_dir, install_dir)
            except OSError:
                if backup_dir.exists():
                    os.replace(backup_dir, install_dir)
                raise
    except OperationError:
        raise
    except (httpx.HTTPError, OSError, tarfile.TarError) as exc:
        raise OperationError(f"could not provision jooservices/server-installer {SERVER_INSTALLER_VERSION} "
                             f"({type(exc).__name__})") from exc
    if not SERVER_INSTALLER.is_file():
        raise OperationError(f"jooservices/server-installer {SERVER_INSTALLER_VERSION} was not installed")


def _run_server_installer(name: str, version: str, packages: list[str] | None = None) -> None:
    command, settings = server_installer_command(name, version)
    if not SERVER_INSTALLER.is_file():
        _ensure_server_installer()
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


def _github_release_asset_matching(repository: str, tag: str, asset_match: re.Pattern[str]) -> tuple[str, bytes]:
    """Download one official GitHub release asset whose metadata publishes a SHA-256 digest."""
    ref = f"tags/{tag}" if tag else "latest"
    response = _response(f"https://api.github.com/repos/{repository}/releases/{ref}", timeout=45)
    try:
        release = response.json()
    except ValueError as exc:
        raise OperationError("official release metadata is invalid") from exc
    matches = [asset for asset in release.get("assets", [])
               if asset_match.fullmatch(str(asset.get("name") or ""))]
    if len(matches) != 1:
        raise OperationError(f"official {repository} release does not have one matching binary")
    asset = matches[0]
    name = str(asset.get("name") or "")
    digest = str(asset.get("digest") or "")
    url = str(asset.get("browser_download_url") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise OperationError(f"official release has no SHA-256 digest for {name}")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise OperationError("official release artifact URL is invalid")
    payload = _response(url, timeout=900).content
    if hashlib.sha256(payload).hexdigest() != digest.removeprefix("sha256:"):
        raise OperationError(f"checksum verification failed for {name}")
    return name, payload


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


def _resolve_node_version(version: str) -> str:
    pin = _check_version(version)
    if pin and _SEMVER.fullmatch(pin):
        return pin.removeprefix("v")
    try:
        index = _response("https://nodejs.org/dist/index.json", timeout=45).json()
    except ValueError as exc:
        raise OperationError("Node.js release index is invalid") from exc
    selected = next((row for row in index if isinstance(row, dict) and isinstance(row.get("version"), str)
                     and (version_matches(row["version"], pin) if pin else row.get("lts"))), None)
    if not selected:
        raise OperationError("Node.js has no release matching versions.lock")
    return str(selected["version"]).removeprefix("v")


def _resolve_composer_version(version: str) -> str:
    pin = _check_version(version)
    if not pin:
        return ""
    try:
        releases = _response("https://getcomposer.org/versions", timeout=45).json().get("stable", [])
    except (ValueError, AttributeError) as exc:
        raise OperationError("Composer release metadata is invalid") from exc
    match = next((str(item.get("version", "")) for item in releases if isinstance(item, dict)
                 and version_matches(item.get("version"), pin)), "")
    if not match:
        raise OperationError("Composer has no stable release matching versions.lock")
    return match.removeprefix("v")


def _resolve_wp_version(version: str) -> str:
    pin = _check_version(version)
    if pin and _SEMVER.fullmatch(pin):
        return pin.removeprefix("v")
    endpoint = ("https://api.github.com/repos/wp-cli/wp-cli/releases/latest" if not pin else
                "https://api.github.com/repos/wp-cli/wp-cli/releases?per_page=100")
    try:
        payload = _response(endpoint, timeout=45).json()
    except ValueError as exc:
        raise OperationError("WP-CLI release metadata is invalid") from exc
    releases = payload if isinstance(payload, list) else [payload]
    for release in releases:
        tag = str((release or {}).get("tag_name", ""))
        match = _SEMVER.fullmatch(tag)
        if match and (not pin or version_matches(tag, pin)):
            return tag.removeprefix("v")
    raise OperationError("WP-CLI has no release matching versions.lock")


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
    normalized = _resolve_node_version(version)
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
    target_version = _resolve_composer_version(version)
    signature = _response("https://composer.github.io/installer.sig", timeout=45).text.strip()
    installer = _response("https://getcomposer.org/installer", timeout=45).content
    if not re.fullmatch(r"[0-9a-f]{96}", signature) or hashlib.sha384(installer).hexdigest() != signature:
        raise OperationError("Composer installer signature verification failed")
    with tempfile.TemporaryDirectory(prefix="aiflow4-composer-") as tmp:
        installer_path = Path(tmp) / "composer-setup.php"
        installer_path.write_bytes(installer)
        command = ["php", str(installer_path), "--install-dir=/usr/local/bin", "--filename=composer"]
        if target_version:
            command.append(f"--version={target_version}")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationError(f"Composer installer could not run ({type(exc).__name__})") from exc
        if result.returncode:
            raise OperationError(f"Composer installer failed (exit {result.returncode})")


def _install_wp(version: str) -> None:
    normalized = _resolve_wp_version(version)
    tag = f"v{normalized}" if normalized else ""
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


_TOOL_HOME = Path("/usr/local/lib/aiflow4/home")
_NPM_CACHE = Path("/usr/local/lib/aiflow4/npm-cache")
_AI_USER = "aiflow-worker"
_TAIL = 400


def _installer_env(**extra: str) -> dict[str, str]:
    """Environment for npm and Playwright installers: works when the node agent has no HOME or npm cache set."""
    env = dict(os.environ)
    fallbacks = []
    if not env.get("HOME"):
        env["HOME"] = str(_TOOL_HOME)
        fallbacks.append(_TOOL_HOME)
    if not env.get("npm_config_cache") and not env.get("NPM_CONFIG_CACHE"):
        env["npm_config_cache"] = str(_NPM_CACHE)
        fallbacks.append(_NPM_CACHE)
    for folder in fallbacks:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    env.update(extra)
    return env


def _output_tail(result: Any) -> str:
    return " ".join(str(result.stderr or result.stdout or "").split())[-_TAIL:]


def _run_installer(label: str, command: list[str], timeout: int, env: dict[str, str]) -> Any:
    """Run an installer; a failure carries the installer's own message (logged and in the error), not only an exit code."""
    import logging
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError(f"{label} could not run ({type(exc).__name__})") from exc
    if result.returncode:
        tail = _output_tail(result)
        logging.getLogger("aiflow4.node").warning("%s failed (exit %s): %s", label, result.returncode, tail)
        raise OperationError(f"{label} failed (exit {result.returncode}): {tail}" if tail
                             else f"{label} failed (exit {result.returncode})")
    return result


def _link_playwright(env: dict[str, str], link_dir: Path = Path("/usr/local/bin")) -> None:
    """Node's global bin folder is only inside its own prefix; link the playwright command into a PATH directory."""
    result = _run_installer("npm prefix lookup", ["npm", "prefix", "--global"], 60, env)
    prefix = Path((result.stdout or "").strip() or "/nonexistent")
    source = prefix / "bin" / "playwright"
    if not (source.exists() or source.is_symlink()):
        raise OperationError(f"Playwright was installed but its command is missing from {prefix / 'bin'}")
    link = link_dir / "playwright"
    if os.path.abspath(source) == os.path.abspath(link):
        return
    temp_link = link.with_name(".playwright.aiflow4")
    try:
        temp_link.unlink(missing_ok=True)
        temp_link.symlink_to(source)
        os.replace(temp_link, link)
    except OSError as exc:
        raise OperationError(f"Playwright command could not be linked into {link_dir} ({type(exc).__name__})") from exc


def _browser_binary(root: Path) -> Path | None:
    for pattern in ("chromium-*/chrome-linux*/chrome",
                    "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell"):
        found = sorted(path for path in root.glob(pattern) if path.is_file())
        if found:
            return found[-1]
    return None


def _check_browser_launch(root: Path) -> str:
    """Launch Chromium headless once as the AI user; return its version, or raise with the missing piece in plain words."""
    import logging
    import pwd

    binary = _browser_binary(root)
    if binary is None:
        raise OperationError(f"Chromium was installed but no browser program was found in {root}")
    prefix: list[str] = []
    if os.geteuid() == 0:
        runuser = shutil.which("runuser")
        if not runuser:
            raise OperationError(f"Chromium cannot be verified as {_AI_USER} because runuser is unavailable")
        prefix = [runuser, "-u", _AI_USER, "--"]
    else:
        try:
            worker_uid = pwd.getpwnam(_AI_USER).pw_uid
        except KeyError as exc:
            raise OperationError(f"Chromium cannot be verified because the {_AI_USER} account is missing") from exc
        if os.geteuid() != worker_uid:
            raise OperationError(f"Chromium must be verified as {_AI_USER}")
    with tempfile.TemporaryDirectory(prefix="aiflow4-browser-") as home:
        os.chmod(home, 0o777)
        env = _installer_env(HOME=home, PLAYWRIGHT_BROWSERS_PATH=str(root))
        try:
            ran = subprocess.run([*prefix, str(binary), "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom",
                                  "about:blank"], capture_output=True, text=True, timeout=60, env=env)
            version = subprocess.run([*prefix, str(binary), "--version"], capture_output=True, text=True,
                                     timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationError(f"Chromium could not be started as {_AI_USER} ({type(exc).__name__})") from exc
    if ran.returncode:
        logging.getLogger("aiflow4.node").warning("Chromium launch check failed (exit %s): %s", ran.returncode,
                                                  _output_tail(ran))
        library = re.search(r"error while loading shared libraries:\s*([^\s:]+)", ran.stderr or "", re.IGNORECASE)
        if library:
            raise OperationError(f"Chromium cannot start: the system library {library.group(1)} is missing")
        raise OperationError(f"Chromium cannot start headless as {_AI_USER} (exit {ran.returncode}): "
                             f"{_output_tail(ran) or 'no message'}")
    return " ".join((version.stdout or version.stderr or "").split()) or "version unknown"


def _install_playwright(version: str) -> None:
    pinned = _check_version(version, required=True)
    env = _installer_env()
    _run_installer("Playwright installer", ["npm", "install", "--global", "--no-audit", "--no-fund", "--registry",
                                            "https://registry.npmjs.org", f"playwright@{pinned}"], 900, env)
    _link_playwright(env)


def _check_playwright_browser_launch(browser: str, root: Path) -> str:
    import logging
    display = {"chromium": "Chromium", "firefox": "Firefox", "webkit": "WebKit"}[browser]
    playwright = shutil.which("playwright")
    node = shutil.which("node")
    if not playwright or not node:
        raise OperationError(f"{display} cannot start: Playwright or Node.js is unavailable")
    package_dir = Path(playwright).resolve().parent
    script = ("const p=require(process.argv[1]); const b=p[process.argv[2]]; "
              "(async()=>{const x=await b.launch({headless:true}); await x.close();})()"
              ".catch(e=>{console.error(e.message||String(e));process.exit(1)})")
    prefix: list[str] = []
    if os.geteuid() == 0 and shutil.which("runuser"):
        prefix = ["runuser", "-u", _AI_USER, "--"]
    with tempfile.TemporaryDirectory(prefix="aiflow4-browser-") as home:
        os.chmod(home, 0o777)
        env = _installer_env(HOME=home, PLAYWRIGHT_BROWSERS_PATH=str(root))
        try:
            result = subprocess.run([*prefix, node, "-e", script, str(package_dir), browser], capture_output=True,
                                    text=True, timeout=60, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationError(f"{display} could not be started as {_AI_USER} ({type(exc).__name__})") from exc
    if result.returncode:
        detail = _output_tail(result) or "no message"
        library = re.search(r"error while loading shared libraries:\s*([^\s:]+)", detail, re.IGNORECASE)
        if library:
            raise OperationError(f"{display} cannot start: system library {library.group(1)} is missing")
        if "Executable doesn't exist" in detail or "Please run" in detail:
            raise OperationError(f"{display} browser files are missing from {root}")
        logging.getLogger("aiflow4.node").warning("%s launch check failed: %s", display, detail)
        raise OperationError(f"{display} cannot start headless as {_AI_USER}: {detail}")
    return f"{display} launch check passed"


def _install_playwright_browser(version: str, browser: str) -> None:
    import logging
    if browser not in {"chromium", "firefox", "webkit"}:
        raise OperationError("browser must be Chromium, Firefox, or WebKit")
    display = {"chromium": "Chromium", "firefox": "Firefox", "webkit": "WebKit"}[browser]
    if not shutil.which("playwright"):
        _install_playwright(version)
    root = Path(PLAYWRIGHT_BROWSERS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    env = _installer_env(PLAYWRIGHT_BROWSERS_PATH=str(root))
    _run_installer(f"Playwright {display} installer", ["playwright", "install", "--with-deps", browser], 1800, env)
    # the AI user runs the browser but root installed it
    subprocess.run(["chmod", "-R", "a+rX", str(root)], capture_output=True, timeout=300)
    reported = _check_playwright_browser_launch(browser, root)
    logging.getLogger("aiflow4.node").info("%s launch check passed: %s", display, reported)


def _install_gitleaks(version: str) -> None:
    arch, _ = _linux_arch()
    asset_arch = {"x86_64": "x64", "aarch64": "arm64"}[arch]
    tag = ""
    if version:
        normalized = _normalized_version(version, "gitleaks")
        tag = f"v{normalized}"
    asset = re.compile(rf"gitleaks_[0-9]+(?:\.[0-9]+){{2}}_linux_{asset_arch}\.tar\.gz")
    _, payload = _github_release_asset_matching("gitleaks/gitleaks", tag, asset)
    _install_binary("gitleaks", _archive_member(payload, "/gitleaks"))


def _install_xdebug(version: str) -> None:
    """Build the official PECL extension for the active PHP CLI and enable it for command-line checks."""
    if not shutil.which("pecl"):
        _run_server_installer("packages", "", ["php-pear", "php-dev", "build-essential"])
    if not shutil.which("pecl"):
        raise OperationError("PECL is unavailable after installing its PHP build prerequisites")
    package = f"xdebug-{_check_version(version)}" if version else "xdebug"
    try:
        result = subprocess.run(["pecl", "install", "--force", package], capture_output=True, text=True,
                               timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError(f"Xdebug installer could not run ({type(exc).__name__})") from exc
    if result.returncode:
        raise OperationError(f"Xdebug installer failed (exit {result.returncode})")
    try:
        config = subprocess.run(["php", "--ini"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError(f"PHP configuration could not be inspected ({type(exc).__name__})") from exc
    match = re.search(r"^Scan for additional \.ini files in:\s*(.+?)\s*$", config.stdout, re.MULTILINE)
    ini_dir = Path(match.group(1)) if match else None
    if config.returncode or not ini_dir or not ini_dir.is_absolute() or not ini_dir.is_dir():
        raise OperationError("PHP has no writable additional ini directory for Xdebug")
    (ini_dir / "99-aiflow-xdebug.ini").write_text("zend_extension=xdebug\n", encoding="utf-8")
    try:
        probe = subprocess.run(["php", "-r", "exit(extension_loaded('xdebug') ? 0 : 1);"],
                               capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError(f"Xdebug preflight could not run ({type(exc).__name__})") from exc
    if probe.returncode:
        raise OperationError("Xdebug was installed but PHP CLI does not load it")


def _install_cosign() -> None:
    arch = COSIGN.get(platform.machine().lower())
    if not arch:
        raise OperationError(f"no pinned cosign for {platform.machine()}")
    payload = _response(f"https://github.com/sigstore/cosign/releases/download/{COSIGN_VERSION}/"
                        f"cosign-linux-{arch[0]}", timeout=900).content
    if hashlib.sha256(payload).hexdigest() != arch[1]:
        raise OperationError("cosign checksum verification failed")
    _install_binary("cosign", payload)


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
    if name == "playwright":
        _install_playwright(version)
        return
    if name in {"chromium", "firefox", "webkit"}:
        _install_playwright_browser(version, name)
        return
    if name == "gitleaks":
        _install_gitleaks(version)
        return
    if name == "xdebug":
        _install_xdebug(version)
        return
    if name == "cosign":
        _install_cosign()
        return
    if name == "podman":
        _run_server_installer("packages", "", ["podman"])
        return

    raise OperationError(f"component is not in the install whitelist: {name}")


def _version_ok(actual: str, expected: str) -> bool:
    return version_matches(actual, expected)


def _tool_version_ok(name: str, actual: str, expected: str) -> bool:
    if name == "codex-code-mode-host":
        if actual.startswith("codex-code-mode-host "):
            return _version_ok(actual, expected)
        return not expected and actual.startswith("present ") and actual != "present "
    return _version_ok(actual, expected)


def prepare_project_toolchains(project_stacks: dict[str, Any], versions: dict[str, Any],
                               required_tools: list[str] | None = None,
                               requested_tools: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Install and preflight every configured project stack on this host.

    The API remains responsible for project admission using the next heartbeat. This routine only installs
    fixed, whitelisted components and returns a fresh local probe so that heartbeat can report convergence.
    """
    if not isinstance(project_stacks, dict) or not isinstance(versions, dict):
        return {"tools": {}, "gaps": {}}
    from .agent import tool_versions

    project_tools: dict[str, list[str]] = {
        str(project): tools_for_stacks_in_project(stacks)
        for project, stacks in project_stacks.items()
    }
    # Older callers passed the targeted repair plan as the third argument. Keep that shape working while
    # heartbeats carry required tools and targeted repairs separately.
    if required_tools and isinstance(required_tools[0], dict):
        requested_tools = [*(requested_tools or []), *required_tools]
        required_tools = []
    allowed = set(NODE_TOOLS) | set(CATALOG_TOOLS) | {tool for values in STACK_TOOLS.values() for tool in values}
    requests = {str(item.get("name", "")): str(item.get("version", ""))
                for item in requested_tools or [] if isinstance(item, dict)
                and str(item.get("name", "")) in allowed}
    required = list(dict.fromkeys([*(required_tools or []),
                                   *(tool for values in project_tools.values() for tool in values), *requests]))
    if not required:
        return {"tools": {}, "gaps": {}}
    before = tool_versions(required)
    per_project_stacks = {str(project): stacks for project, stacks in project_stacks.items()}
    selected = set(toolchain_gaps(before, required_tools or [], versions))
    for project, tools in project_tools.items():
        selected.update(toolchain_gaps(before, tools, versions, per_project_stacks[project], require_pins=True))
    selected.update(tool for tool, version in requests.items()
                    if not _tool_version_ok(tool, str(before.get(tool, "missing")), version))
    ordered = sorted(selected, key=lambda tool: (
        _INSTALL_ORDER.index(tool) if tool in _INSTALL_ORDER else len(_INSTALL_ORDER), tool))
    repaired: list[str] = []
    for tool in ordered:
        stacks = next((per_project_stacks[project] for project, tools in project_tools.items() if tool in tools), [])
        stack_tools = {required for stack in (stacks if isinstance(stacks, list) else [stacks])
                       for required in STACK_TOOLS.get(str(stack), ())}
        requested = tool in requests
        request_version = requests.get(tool, "")
        if tool in stack_tools and not (request_version or locked_version(versions, tool)):
            continue
        version = request_version if requested else pinned_version(versions, tool, stacks)
        try:
            _install_component(tool, version, [])
            repaired.append(tool)
        except Exception as exc:  # noqa: BLE001 - keep preparing unrelated tools/projects after one installer fails
            import logging
            logging.getLogger("aiflow4.node").warning("toolchain install failed for %s: %s", tool, type(exc).__name__)
    after = tool_versions(required)
    gaps = {project: toolchain_gaps(after, tools, versions, per_project_stacks[project], require_pins=True)
            for project, tools in project_tools.items()}
    request_gaps = [tool for tool, version in requests.items()
                    if not _tool_version_ok(tool, str(after.get(tool, "missing")), version)]
    system_gaps = set(toolchain_gaps(after, required_tools or [], versions))
    installed = [tool for tool in repaired if tool not in system_gaps and tool not in request_gaps
                 and not any(tool in project_gaps for project_gaps in gaps.values())]
    return {"tools": after, "gaps": gaps, "installed": installed, "request_gaps": request_gaps}


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


def _restore_runtime_file(path: Path, previous: bytes | None) -> None:
    """Restore an env file atomically without exposing its contents in diagnostics."""
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.restore")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(previous)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _wait_for_worker_idle(units: list[str], timeout_s: int = 3900) -> bool:
    """Wait for targeted workers to finish; only restart after a stable idle window."""
    from .agent import worker_states

    instances = {unit.split("@", 1)[1].removesuffix(".service") for unit in units}
    started = time.monotonic()
    idle_since: float | None = None
    while time.monotonic() - started < timeout_s:
        active = any(str(item.get("instance") or item.get("account") or "") in instances
                     and item.get("state") in {"working", "probing"} and item.get("alive", True)
                     for item in worker_states())
        if active:
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= 5:
            return True
        time.sleep(1)
    return False


def _restart_worker(unit: str) -> bool:
    if not _SAFE_UNIT.fullmatch(unit):
        raise OperationError("runtime setting names an invalid worker service")
    try:
        active = subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if active.returncode:
        return True  # stopped workers read the changed env file when they start again
    try:
        restarted = subprocess.run(["systemctl", "restart", unit], capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if restarted.returncode:
        return False
    try:
        healthy = subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return healthy.returncode == 0


def _control_compose_file() -> tuple[Path, dict[str, str]]:
    """Resolve the compose file and image tag from an existing API container, never from request text."""
    config = subprocess.run(
        ["docker", "inspect", "--format", '{{ index .Config.Labels "com.docker.compose.project.config_files" }}',
         "aiflow4-control-api-a"], capture_output=True, text=True, timeout=30)
    image = subprocess.run(["docker", "inspect", "--format", "{{.Config.Image}}", "aiflow4-control-api-a"],
                           capture_output=True, text=True, timeout=30)
    if config.returncode or image.returncode:
        raise OperationError("control API container could not be inspected")
    raw = (config.stdout or "").strip().split(",", 1)[0]
    try:
        resolved = Path(raw).resolve(strict=True)
    except OSError:
        raise OperationError("control compose file is unavailable") from None
    build_root = Path("/srv/aiflow/build").resolve(strict=False)
    if (not resolved.is_relative_to(build_root) or resolved.name != "compose.yaml"
            or resolved.parts[-4:] != ("deploy", "v4", "control", "compose.yaml")):
        raise OperationError("control compose file is outside the installed release")
    image_ref = (image.stdout or "").strip()
    if ":" not in image_ref:
        raise OperationError("control API image tag could not be determined")
    repository, tag = image_ref.rsplit(":", 1)
    return resolved, {"AIFLOW4_IMAGE_REPOSITORY": repository, "AIFLOW4_IMAGE_TAG": tag,
                      "AIFLOW4_VERSION": tag}


def _control_api_healthy(service: str, compose: Path, env: dict[str, str]) -> bool:
    try:
        result = subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d", "--no-deps", service],
                                capture_output=True, timeout=180, env={**os.environ, **env})
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    container = f"aiflow4-{service}"
    for _ in range(40):
        try:
            health = subprocess.run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if health.returncode == 0 and (health.stdout or "").strip() == "healthy":
            return True
        time.sleep(2)
    return False


def _runtime_setting_target(payload: dict[str, Any]) -> tuple[str, str, str, str, Path]:
    """Validate a runtime setting request and resolve the env file it writes."""
    scope = str(payload.get("scope") or "")
    project = str(payload.get("project") or "")
    key, value = str(payload.get("key") or ""), str(payload.get("value") or "")
    if (scope not in {"secret", "project"} or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)
            or not re.fullmatch(r"[\x21-\x7e]{1,4096}", value)):
        raise OperationError("runtime setting request is invalid")
    if scope == "secret" and ("'" in value or "\\" in value):
        raise OperationError("control runtime value has an unsupported format")
    if scope == "project":
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", project):
            raise OperationError("runtime project name is invalid")
        path = PROJECT_ENV_DIR / f"{project}.env"
    else:
        path = RUNTIME_SECRET_FILE
    if path.is_symlink():
        raise OperationError("runtime env file is not a regular file")
    return scope, project, key, value, path


def _failed_rollback_services(scope: str, attempted: list[str]) -> list[str]:
    """Restart what the failed change touched with the previous value, returning the services that stayed unhealthy."""
    rollback_failures: list[str] = []
    if scope == "project":
        for unit in attempted:
            if not _restart_worker(unit):
                rollback_failures.append(unit)
    else:
        try:
            compose, env = _control_compose_file()
            for service in ("control-api-a", "control-api-b"):
                if not _control_api_healthy(service, compose, env):
                    rollback_failures.append(service)
        except Exception:
            rollback_failures.extend([service for service in ("control-api-a", "control-api-b")
                                      if service not in rollback_failures])
    return rollback_failures


def _execute_runtime_setting(payload: dict[str, Any],
                             checkpoint: Callable[[dict[str, str]], dict[str, str]] | None = None) -> dict[str, Any]:
    """Write one validated env entry, restart fixed services, and restore the prior file on failure."""
    from ..install.secrets import update_env

    scope, project, key, value, path = _runtime_setting_target(payload)
    try:
        rollback_context = f"{payload.get('change_id')}:rollback:{payload.get('operation_id')}"
        from ..core.runtime_settings_crypto import decrypt_value, encrypt_value, node_public_key
        rollback_envelope = payload.get("rollback_envelope")
        if not isinstance(rollback_envelope, dict):
            previous = path.read_bytes() if path.is_file() else None
            snapshot = json.dumps({"exists": previous is not None,
                                  "content": base64.b64encode(previous or b"").decode("ascii")},
                                 separators=(",", ":"))
            rollback_envelope = encrypt_value(node_public_key(), snapshot,
                                              f"runtime-setting-rollback:{rollback_context}")
            if checkpoint is not None:
                rollback_envelope = checkpoint(rollback_envelope)
        snapshot = json.loads(decrypt_value(rollback_envelope, f"runtime-setting-rollback:{rollback_context}"))
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("exists"), bool):
            raise ValueError("invalid rollback snapshot")
        previous_content = base64.b64decode(str(snapshot.get("content") or ""), validate=True)
        previous = previous_content if snapshot["exists"] else None
        update_env(path, force={key: value}, header="Managed from the AI Flow Settings screen",
                   track_timestamps=True, compose_literal=scope == "secret")
    except OSError:
        raise OperationError("runtime env file could not be written") from None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise OperationError("runtime setting rollback snapshot could not be secured") from None

    completed: list[str] = []
    attempted: list[str] = []
    try:
        if scope == "project":
            units = payload.get("units") or []
            if not isinstance(units, list) or any(not isinstance(unit, str) for unit in units):
                raise OperationError("runtime worker service plan is invalid")
            if not _wait_for_worker_idle(units):
                raise OperationError("active worker did not become idle; no service was restarted")
            for unit in units:
                attempted.append(unit)
                if not _restart_worker(unit):
                    raise OperationError(f"service {unit} failed its restart health check")
                completed.append(unit)
        else:
            try:
                compose, env = _control_compose_file()
            except Exception as exc:
                raise OperationError(f"service control-api-a could not be restarted ({type(exc).__name__})") from None
            for service in ("control-api-a", "control-api-b"):
                if not _control_api_healthy(service, compose, env):
                    raise OperationError(f"service {service} failed its restart health check")
                completed.append(service)
    except Exception as exc:
        try:
            _restore_runtime_file(path, previous)
        except OSError:
            raise OperationError("runtime setting failed and its previous value could not be restored") from None
        rollback_failures = _failed_rollback_services(scope, attempted)
        if rollback_failures:
            raise OperationError(f"{exc}; previous value restored but rollback health failed for "
                                 f"{', '.join(rollback_failures)}") from None
        if isinstance(exc, OperationError):
            raise OperationError(f"{exc}; previous value restored") from None
        raise OperationError("runtime service restart failed; previous value restored") from None
    return {"healthy": True, "key": key, "scope": scope, "project": project,
            "services": completed, "set": True, "rollback_envelope": rollback_envelope}


def _execute_runtime_setting_rollback(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore a peer's encrypted pre-change file snapshot and health-check its affected services."""
    scope = str(payload.get("scope") or "")
    project = str(payload.get("project") or "")
    key = str(payload.get("key") or "")
    if (scope not in {"secret", "project"} or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)):
        raise OperationError("runtime setting rollback request is invalid")
    if scope == "project":
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", project):
            raise OperationError("runtime project name is invalid")
        path = PROJECT_ENV_DIR / f"{project}.env"
    else:
        path = RUNTIME_SECRET_FILE
    if path.is_symlink():
        raise OperationError("runtime env file is not a regular file")
    try:
        snapshot = payload.get("snapshot") or {}
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("exists"), bool):
            raise ValueError
        content = base64.b64decode(str(snapshot.get("content") or ""), validate=True)
        _restore_runtime_file(path, content if snapshot["exists"] else None)
    except (ValueError, OSError):
        raise OperationError("previous value could not be restored") from None

    if scope == "project":
        units = payload.get("units") or []
        if not isinstance(units, list) or any(not isinstance(unit, str) for unit in units):
            raise OperationError("runtime worker rollback service plan is invalid")
        if not _wait_for_worker_idle(units):
            raise OperationError("previous value restored but active workers did not become idle for rollback")
        for unit in units:
            if not _restart_worker(unit):
                raise OperationError(f"previous value restored but service {unit} failed its rollback health check")
        services = units
    else:
        try:
            compose, env = _control_compose_file()
        except Exception as exc:
            raise OperationError(f"previous value restored but control API rollback could not start ({type(exc).__name__})") from None
        services = []
        for service in ("control-api-a", "control-api-b"):
            if not _control_api_healthy(service, compose, env):
                raise OperationError(f"previous value restored but service {service} failed its rollback health check")
            services.append(service)
    return {"healthy": True, "key": key, "scope": scope, "project": project,
            "services": services, "restored": True}


def _restart_worker_operation(payload: dict[str, Any]) -> dict[str, Any]:
    """Restart one worker service by account id, after checking the id against the allowed shape."""
    account = str(payload.get("account") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", account):
        raise OperationError("worker account id is invalid")
    try:
        result = subprocess.run(["systemctl", "restart", f"aiflow4-worker@{account}.service"],
                                capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError(f"worker restart could not run ({type(exc).__name__})") from exc
    if result.returncode:
        raise OperationError(f"systemd could not restart worker {account} (exit {result.returncode})")
    return {"healthy": True, "action": "restart_worker", "account": account}


def _decrypt_rollback_snapshot(payload: dict[str, Any]) -> Any:
    """Decrypt the pre-change file snapshot a runtime-setting rollback restores."""
    try:
        from ..core.runtime_settings_crypto import decrypt_value
        envelope = payload.get("rollback_envelope")
        context = str(payload.get("rollback_context") or "")
        if not isinstance(envelope, dict) or not context:
            raise ValueError("missing rollback envelope")
        return json.loads(decrypt_value(envelope, f"runtime-setting-rollback:{context}"))
    except Exception:
        raise OperationError("runtime setting rollback snapshot could not be decrypted on this machine") from None


def execute(operation: dict[str, Any],
            checkpoint: Callable[[dict[str, str]], dict[str, str]] | None = None) -> dict[str, Any]:
    """Run a whitelist-bound install, repair, worker-restart or runtime-setting operation created by control."""
    payload = operation.get("payload") or {}
    from .agent import tool_versions

    action = payload.get("action")
    if action == "restart_worker":
        return _restart_worker_operation(payload)
    if action in {"runtime_setting_project", "runtime_setting_control"}:
        expected_scope = "project" if action == "runtime_setting_project" else "secret"
        if payload.get("scope") != expected_scope:
            raise OperationError("runtime setting operation scope is invalid")
        try:
            from ..core.runtime_settings_crypto import decrypt_value
            envelope = payload.get("value_envelope")
            if not isinstance(envelope, dict):
                raise ValueError("missing envelope")
            decrypted = {**payload, "operation_id": str(operation.get("id") or ""),
                         "value": decrypt_value(envelope, str(payload.get("change_id") or ""))}
        except Exception:
            raise OperationError("runtime setting value could not be decrypted on this machine") from None
        return _execute_runtime_setting(decrypted, checkpoint=checkpoint)
    if action == "runtime_setting_rollback":
        return _execute_runtime_setting_rollback({**payload, "snapshot": _decrypt_rollback_snapshot(payload)})
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
    needs_npm = any(str(item.get("name", "")) in AI_PACKAGES for item in ordered)
    npm_missing = needs_npm and shutil.which("npm") is None
    for item in ordered:
        name = str(item.get("name", ""))
        version = _check_version(str(item.get("version", "")), required=name in AI_PACKAGES)
        packages = item.get("packages") or []
        if not isinstance(packages, list):
            raise OperationError("package plan is invalid")
        prerequisite_missing = name == "node" and npm_missing
        if (name != "packages" and not prerequisite_missing
                and _tool_version_ok(name, str(before.get(name, "missing")), version)):
            continue
        _install_component(name, version, packages)
        installed.append(name)
        if name == "node":
            npm_missing = False

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
