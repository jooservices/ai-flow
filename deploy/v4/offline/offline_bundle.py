#!/usr/bin/env python3
"""Build and install a checksummed, architecture-specific offline AI Flow v4 node bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_ROOT = REPO_ROOT / "deploy/v4"
PACKAGE_NAMES = {
    "claude_code": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "opencode": "opencode-ai",
    "grok": "@vibe-kit/grok-cli",
}
ARCHES = {"x86_64": ("amd64", "x64", "x86_64"), "aarch64": ("arm64", "arm64", "aarch64")}
# Newest glibc a bundled wheel may require; Ubuntu 22.04 (2.35), 24.04 (2.39) and Debian 12 (2.36) all exceed it.
MANYLINUX_GLIBC_FLOOR = (2, 28)
REQUIRED_TOOLS = ("claude_code", "codex", "opencode", "node", "uv", "python3")
AGENT_UNIT = "aiflow4-node-agent.service"
WORKER_UNIT = "aiflow4-worker@.service"


class BundleError(RuntimeError):
    """Invalid bundle input or failed offline install step."""


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         input: str | None = None, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=cwd, env=env, input=input, text=True, capture_output=capture_output)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise BundleError(f"command failed ({result.returncode}): {' '.join(args)}{': ' + detail if detail else ''}")
    return result


def _version(value: Any, name: str, *, allow_minor: bool = False) -> str:
    if isinstance(value, dict):
        value = value.get("version")
    if not isinstance(value, (str, int, float)):
        raise BundleError(f"versions.lock is missing tools.{name}")
    result = str(value).strip().removeprefix("v")
    pattern = r"[0-9]+\.[0-9]+(?:\.[0-9]+)?" if allow_minor else r"[0-9]+\.[0-9]+\.[0-9]+"
    if not re.fullmatch(pattern, result):
        raise BundleError(f"tools.{name} must be pinned to MAJOR.MINOR" + (".PATCH" if not allow_minor else ""))
    return result


def read_versions_lock(path: Path, include_grok: bool = False) -> dict[str, str]:
    """Read the same aliases as AI Flow's toolchain map and require exact offline pins."""
    import yaml
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise BundleError(f"could not read versions.lock: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("tools"), dict):
        raise BundleError("versions.lock must contain a tools mapping")
    tools = document["tools"]
    result: dict[str, str] = {}
    for name in REQUIRED_TOOLS:
        aliases = (("claude_code", "claude-code", "claude") if name == "claude_code" else
                   ("python3", "python") if name == "python3" else (name,))
        for key in aliases:
            if key in tools:
                result[name] = _version(tools[key], key, allow_minor=name == "python3")
                break
        else:
            raise BundleError(f"versions.lock is missing the required {name} pin")
    if result["python3"] != "3.12" and not result["python3"].startswith("3.12."):
        raise BundleError("tools.python3 must pin Python 3.12.x for AI Flow v4")
    if include_grok:
        result["grok"] = _version(tools.get("grok"), "grok")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_docker_archive(path: Path, image_ref: str, architecture: str) -> None:
    try:
        with tarfile.open(path, "r:*") as archive:
            member = archive.extractfile("manifest.json")
            if member is None:
                raise BundleError("Docker image archive has no manifest.json (create it with docker save)")
            manifest = json.load(member)
            selected = next((image for image in manifest
                             if isinstance(image, dict) and image_ref in image.get("RepoTags", [])), None)
            if selected is None:
                raise BundleError(f"Docker image archive does not contain {image_ref}")
            config = archive.extractfile(selected["Config"])
            image_config = json.load(config) if config is not None else {}
    except (KeyError, OSError, ValueError, tarfile.TarError) as exc:
        raise BundleError("Docker image archive has no readable image configuration") from exc
    expected = "amd64" if architecture == "x86_64" else "arm64"
    if image_config.get("architecture") != expected:
        raise BundleError(f"Docker image is {image_config.get('architecture')}, expected {expected}")


def _copy_command(command: str, destination: Path) -> Path:
    candidate = Path(command)
    resolved = shutil.which(command) or (str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None)
    if not resolved:
        raise BundleError(f"required build command is missing: {command}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(resolved).resolve(), destination)
    destination.chmod(0o755)
    return destination


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["curl", "--fail", "--location", "--silent", "--show-error", "--max-time", "240", url,
          "--output", str(destination)])


def _validate_tar_members(members: list[tarfile.TarInfo]) -> None:
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise BundleError("archive contains an unsafe path")
        if member.issym() or member.islnk():
            link = PurePosixPath(member.linkname)
            if link.is_absolute():
                raise BundleError("archive contains an unsafe link")
            base = PurePosixPath(member.name).parent if member.issym() else PurePosixPath()
            resolved = posixpath.normpath(str(base / link))
            if resolved == ".." or resolved.startswith("../"):
                raise BundleError("archive contains a link outside its root")
        if member.isdev() or member.isfifo():
            raise BundleError("archive contains an unsupported special file")


def _extract_release(release: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(release, "r:*") as archive:
            _validate_tar_members(archive.getmembers())
            archive.extractall(destination, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    except (OSError, tarfile.TarError) as exc:
        raise BundleError(f"could not unpack release archive: {exc}") from exc


def _node_archive(version: str, target: tuple[str, str, str], payload: Path, work: Path) -> None:
    deb_arch, node_arch, _ = target
    del deb_arch
    base = f"node-v{version}-linux-{node_arch}"
    sums = work / "node-SHASUMS256.txt"
    archive_path = work / f"{base}.tar.xz"
    _download(f"https://nodejs.org/dist/v{version}/SHASUMS256.txt", sums)
    _download(f"https://nodejs.org/dist/v{version}/{base}.tar.xz", archive_path)
    expected = None
    for line in sums.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == archive_path.name:
            expected = fields[0]
            break
    if not expected or _sha256(archive_path) != expected:
        raise BundleError("Node.js archive checksum verification failed")
    extracted = work / "node"
    extracted.mkdir()
    _run(["tar", "-xJf", str(archive_path), "-C", str(extracted)])
    source = extracted / base
    if not (source / "bin/node").is_file() or not (source / "bin/npm").is_file():
        raise BundleError("Node.js archive is missing node or npm")
    shutil.copytree(source, payload / "node", symlinks=True)


def _install_cli_packages(versions: dict[str, str], payload: Path, work: Path, include_grok: bool) -> None:
    env = os.environ.copy()
    node_bin = payload / "node/bin"
    env["PATH"] = f"{node_bin}:{env.get('PATH', '')}"
    env["npm_config_update_notifier"] = "false"
    prefix = payload / "cli"
    prefix.mkdir(parents=True)
    packages = [f"{PACKAGE_NAMES[name]}@{versions[name]}" for name in ("claude_code", "codex", "opencode")]
    if include_grok:
        packages.append(f"{PACKAGE_NAMES['grok']}@{versions['grok']}")
    npm = str(node_bin / "npm")
    _run([npm, "install", "--global", "--prefix", str(prefix), "--no-audit", "--no-fund", *packages],
         cwd=work, env=env)
    package_paths = {
        "claude_code": prefix / "lib/node_modules/@anthropic-ai/claude-code/package.json",
        "codex": prefix / "lib/node_modules/@openai/codex/package.json",
        "opencode": prefix / "lib/node_modules/opencode-ai/package.json",
        "grok": prefix / "lib/node_modules/@vibe-kit/grok-cli/package.json",
    }
    for name in ("claude_code", "codex", "opencode", *(('grok',) if include_grok else ())):
        metadata = package_paths[name]
        try:
            installed = json.loads(metadata.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError) as exc:
            raise BundleError(f"npm did not install the locked package for {name}") from exc
        if installed != versions[name]:
            raise BundleError(f"installed {name} version {installed} does not match versions.lock ({versions[name]})")

    code_host_arch = ARCHES[platform.machine()][2]
    host_name = f"codex-code-mode-host-{code_host_arch}-unknown-linux-musl.tar.gz"
    host_url = (f"https://github.com/openai/codex/releases/download/rust-v{versions['codex']}/{host_name}")
    host_archive = work / host_name
    _download(host_url, host_archive)
    host_root = work / "codex-host"
    host_root.mkdir()
    _run(["tar", "-xzf", str(host_archive), "-C", str(host_root)])
    host = next(host_root.rglob("codex-code-mode-host"), None)
    if not host or not host.is_file():
        raise BundleError("Codex release archive has no codex-code-mode-host binary")
    codex_bin = prefix / "lib/node_modules/@openai/codex/bin"
    codex_bin.mkdir(parents=True, exist_ok=True)
    shutil.copy2(host, codex_bin / "codex-code-mode-host")
    (codex_bin / "codex-code-mode-host").chmod(0o755)
    (codex_bin / "codex-code-mode-host.version").write_text(versions["codex"] + "\n", encoding="utf-8")


def _build_wheelhouse(release_root: Path, versions: dict[str, str], payload: Path, work: Path,
                      target: tuple[str, str, str]) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise BundleError("uv is required to build the offline Python wheelhouse")
    uv_version = _run([uv, "--version"]).stdout.strip().split()
    if len(uv_version) < 2 or uv_version[1].removeprefix("v") != versions["uv"]:
        raise BundleError(f"builder uv version must match versions.lock ({versions['uv']})")
    uv_target = _copy_command(uv, payload / "bin/uv")
    python_root = payload / "python/managed"
    python_root.mkdir(parents=True)
    python_env = os.environ.copy()
    python_env["UV_PYTHON_INSTALL_DIR"] = str(python_root)
    _run([str(uv_target), "python", "install", "--install-dir", str(python_root), versions["python3"]],
         cwd=release_root, env=python_env)
    python_paths = list(python_root.rglob("python3.12"))
    python_paths = [path for path in python_paths if path.is_file() and os.access(path, os.X_OK)]
    if not python_paths:
        raise BundleError("uv did not install the locked Python 3.12 runtime")
    python_bin = payload / "python/bin"
    python_bin.mkdir(parents=True)
    relative = os.path.relpath(python_paths[0], python_bin)
    (python_bin / "python3.12").symlink_to(relative)
    (python_bin / "python3").symlink_to("python3.12")
    reported = _run([str(python_bin / "python3.12"), "--version"], env=python_env).stdout.strip()
    if versions["python3"] not in reported:
        raise BundleError(f"managed Python version {reported!r} does not match versions.lock")

    requirements = work / "requirements.txt"
    _run([str(uv_target), "export", "--locked", "--no-dev", "--no-emit-project", "--no-emit-index-url",
          "--no-emit-find-links", "--format", "requirements-txt",
          "--output-file", str(requirements)], cwd=release_root, env=python_env)
    wheelhouse = payload / "wheelhouse"
    wheelhouse.mkdir()
    _run([str(uv_target), "build", "--wheel", "--out-dir", str(wheelhouse)], cwd=release_root, env=python_env)
    _, _, node_arch = target
    pip_python = shutil.which("python3")
    if not pip_python:
        raise BundleError("python3 is required to download locked wheels (install python3-pip on the build host)")
    _run(pip_download_command(pip_python, requirements, wheelhouse, node_arch), cwd=release_root)
    shutil.copy2(requirements, payload / "requirements.txt")


def manylinux_platforms(node_arch: str) -> list[str]:
    """Every manylinux tag a node at the bundle's glibc floor accepts, newest first.

    pip matches ``--platform`` exactly, so each compatible tag must be listed: locked wheels such as
    ``pydantic_core`` or ``psycopg_binary`` ship only ``manylinux_2_17``/``manylinux2014`` tags.
    """
    machine = "x86_64" if node_arch == "x64" else "aarch64"
    tags = [f"manylinux_2_{minor}_{machine}" for minor in range(MANYLINUX_GLIBC_FLOOR[1], 16, -1)]
    return [*tags, f"manylinux2014_{machine}"]


def pip_download_command(pip_python: str, requirements: Path, wheelhouse: Path, node_arch: str) -> list[str]:
    platforms = [flag for tag in manylinux_platforms(node_arch) for flag in ("--platform", tag)]
    return [pip_python, "-m", "pip", "download", "--only-binary=:all:", "--require-hashes",
            "--dest", str(wheelhouse), *platforms, "--python-version", "312",
            "--implementation", "cp", "--abi", "cp312", "--requirement", str(requirements)]


def _deb_control(package: str, version: str, description: str) -> str:
    return (f"Package: {package}\nVersion: {version}\nSection: admin\nPriority: optional\n"
            f"Architecture: all\nDepends: systemd, passwd\nMaintainer: AI Flow <aiflow@jooservices.com>\n"
            f"Description: {description}\n")


def build_deb_packages(destination: Path, release_version: str) -> list[Path]:
    """Build architecture-independent systemd packages with the node user and data layout."""
    version = release_version.removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise BundleError("release version must be vX.Y.Z")
    destination.mkdir(parents=True, exist_ok=True)
    package_root = destination.parent / ".deb-build"
    shutil.rmtree(package_root, ignore_errors=True)
    packages = (
        ("aiflow4-node-agent", AGENT_UNIT, "AI Flow v4 node agent service"),
        ("aiflow4-worker", WORKER_UNIT, "AI Flow v4 worker systemd template"),
    )
    output: list[Path] = []
    for name, unit, description in packages:
        root = package_root / name
        (root / "DEBIAN").mkdir(parents=True)
        _run(["install", "-D", "-m", "0644", str(DEPLOY_ROOT / "node/systemd" / unit),
              str(root / "lib/systemd/system" / unit)])
        dropin = root / "etc/systemd/system" / f"{unit}.d/offline.conf"
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(
            '[Service]\n'
            'Environment="PATH=/opt/aiflow/offline/current/bin:/opt/aiflow/offline/current/python/bin:'
            '/opt/aiflow/offline/current/node/bin:/opt/aiflow/offline/current/cli/bin:'
            '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
            'Environment=UV_PYTHON_INSTALL_DIR=/opt/aiflow/offline/current/python/managed\n',
            encoding="utf-8")
        (root / "DEBIAN/control").write_text(_deb_control(name, version, description), encoding="utf-8")
        postinst = """#!/bin/sh
set -e
if ! getent passwd aiflow-worker >/dev/null; then
  useradd --system --user-group --create-home --home-dir /srv/aiflow --shell /usr/sbin/nologin aiflow-worker
fi
install -d -m 0755 /etc/aiflow /srv/aiflow/accounts /srv/aiflow/cache/ai/tmp /srv/aiflow/work
install -d -o aiflow-worker -g aiflow-worker -m 0750 /srv/aiflow/accounts /srv/aiflow/cache/ai/tmp /srv/aiflow/work
install -d -o root -g root -m 0700 /srv/aiflow/state
systemctl daemon-reload >/dev/null 2>&1 || true
"""
        if name == "aiflow4-node-agent":
            postinst += "systemctl enable aiflow4-node-agent.service >/dev/null 2>&1 || true\n"
        postinst += "exit 0\n"
        postrm = "#!/bin/sh\nset -e\nsystemctl daemon-reload >/dev/null 2>&1 || true\nexit 0\n"
        (root / "DEBIAN/postinst").write_text(postinst, encoding="utf-8")
        (root / "DEBIAN/postinst").chmod(0o755)
        (root / "DEBIAN/postrm").write_text(postrm, encoding="utf-8")
        (root / "DEBIAN/postrm").chmod(0o755)
        deb = destination / f"{name}_{version}_all.deb"
        _run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb)])
        output.append(deb)
    shutil.rmtree(package_root, ignore_errors=True)
    return output


def _write_checksums(bundle_root: Path) -> None:
    rows = []
    for path in sorted(p for p in bundle_root.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        rows.append(f"{_sha256(path)}  {path.relative_to(bundle_root).as_posix()}")
    (bundle_root / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")


def build_bundle(*, version: str, release: Path, image: Path, image_ref: str, versions_lock: Path,
                 output: Path, arch: str | None = None, include_grok: bool = False) -> Path:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise BundleError("release version must be vX.Y.Z")
    if not release.is_file() or not image.is_file() or not versions_lock.is_file():
        raise BundleError("release, image tar, and aiflow-control versions.lock must all be readable files")
    machine = platform.machine()
    if machine not in ARCHES:
        raise BundleError(f"unsupported builder architecture: {machine}")
    host_arch = ARCHES[machine][0]
    target_arch = arch or host_arch
    if target_arch != host_arch:
        raise BundleError("build on a host matching the target architecture so npm and Python artifacts are native")
    if image_ref.rsplit(":", 1)[-1] != version:
        raise BundleError("image-ref must be tagged with the selected release version")
    _verify_docker_archive(image, image_ref, machine)
    versions = read_versions_lock(versions_lock, include_grok)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aiflow4-offline-build-") as temporary:
        work = Path(temporary)
        root_name = f"aiflow4-offline-{version}-{target_arch}"
        bundle_root = work / root_name
        payload = bundle_root / "payload"
        payload.mkdir(parents=True)
        assets = bundle_root / "assets"
        assets.mkdir()
        shutil.copy2(release, assets / f"aiflow4-{version}.tar.gz")
        shutil.copy2(image, assets / "control-image.tar")
        shutil.copy2(versions_lock, assets / "versions.lock")
        release_root = work / "release"
        _extract_release(release, release_root)
        if not (release_root / "uv.lock").is_file() or not (release_root / "pyproject.toml").is_file():
            raise BundleError("release archive must contain pyproject.toml and uv.lock")

        target = ARCHES[machine]
        _node_archive(versions["node"], target, payload, work)
        _install_cli_packages(versions, payload, work, include_grok)
        _build_wheelhouse(release_root, versions, payload, work, target)
        debs = build_deb_packages(bundle_root / "packages", version)
        shutil.copy2(Path(__file__), bundle_root / "offline_bundle.py")
        manifest = {
            "format": 1,
            "release": version,
            "architecture": target_arch,
            "image": image_ref,
            "versions": versions,
            "grok_included": bool(include_grok),
            "files": {
                "release": f"assets/aiflow4-{version}.tar.gz",
                "image": "assets/control-image.tar",
                "versions_lock": "assets/versions.lock",
                "requirements": "payload/requirements.txt",
                "wheelhouse": "payload/wheelhouse",
                "packages": [f"packages/{deb.name}" for deb in debs],
            },
            "checksums": "SHA256SUMS",
        }
        (bundle_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                                    encoding="utf-8")
        _write_checksums(bundle_root)
        temporary_output = output.with_suffix(output.suffix + ".new")
        with tarfile.open(temporary_output, "w:gz") as archive:
            archive.add(bundle_root, arcname=root_name, recursive=True)
        os.replace(temporary_output, output)
        output.with_suffix(output.suffix + ".sha256").write_text(
            f"{_sha256(output)}  {output.name}\n", encoding="utf-8")
    return output


def _safe_bundle_path(bundle: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise BundleError("bundle manifest contains an unsafe path")
    result = bundle.joinpath(*path.parts)
    if not result.exists():
        raise BundleError(f"bundle is missing {relative}")
    if not result.resolve().is_relative_to(bundle.resolve()):
        raise BundleError(f"bundle path escapes the bundle directory: {relative}")
    return result


def verify_bundle(bundle: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        checksum_lines = (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        raise BundleError(f"bundle manifest or checksum file is missing/invalid: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise BundleError("offline bundle manifest must be a JSON object with a files mapping")
    if (manifest.get("format") != 1
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", str(manifest.get("release", "")))
            or manifest.get("architecture") not in ("amd64", "arm64")):
        raise BundleError("unsupported or invalid offline bundle manifest")
    required_file_keys = {"release", "image", "versions_lock", "requirements", "wheelhouse", "packages"}
    if not required_file_keys.issubset(manifest["files"]):
        raise BundleError("offline bundle manifest is missing required component paths")
    if not isinstance(manifest["files"]["packages"], list) or len(manifest["files"]["packages"]) != 2:
        raise BundleError("offline bundle manifest must include both Debian packages")
    checked: set[str] = set()
    for line in checksum_lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise BundleError("invalid SHA256SUMS row")
        relative = parts[1]
        path = _safe_bundle_path(bundle, relative)
        if path.is_dir() or _sha256(path) != parts[0]:
            raise BundleError(f"checksum verification failed for {relative}")
        checked.add(relative)
    actual_files = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*")
                    if path.is_file() and path.name != "SHA256SUMS"}
    if actual_files != checked:
        raise BundleError("SHA256SUMS must cover every regular bundle file")
    required = {"manifest.json", "offline_bundle.py", "assets/versions.lock", manifest.get("files", {}).get("release"),
                manifest.get("files", {}).get("image"), manifest.get("files", {}).get("requirements")}
    if not required.issubset(checked):
        raise BundleError("SHA256SUMS does not cover every required bundle component")
    for relative in manifest.get("files", {}).get("packages", []):
        if relative not in checked:
            raise BundleError("SHA256SUMS does not cover both Debian packages")
        _safe_bundle_path(bundle, relative)
    for relative in (manifest["files"]["release"], manifest["files"]["image"]):
        _safe_bundle_path(bundle, relative)
    wheelhouse = _safe_bundle_path(bundle, manifest["files"]["wheelhouse"])
    if not wheelhouse.is_dir() or not list(wheelhouse.glob("*.whl")):
        raise BundleError("offline bundle has no Python wheels")
    return manifest


def _parse_node_env(path: Path, root: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BundleError("/etc/aiflow/node.env must be configured before offline install") from exc
    values: dict[str, str] = {}
    import shlex
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise BundleError("node.env contains an invalid setting") from exc
        if not parts or "=" not in parts[0]:
            continue
        key, value = parts[0].split("=", 1)
        if re.fullmatch(r"AIFLOW4_[A-Z0-9_]+", key):
            values[key] = value
    control_url = values.get("AIFLOW4_CONTROL_URL", "").rstrip("/")
    node_name = values.get("AIFLOW4_NODE_NAME", "")
    slots = values.get("AIFLOW4_SLOTS", "")
    token_file = values.get("AIFLOW4_NODE_TOKEN_FILE", "/srv/aiflow/state/node.token")
    if not re.fullmatch(r"https?://[A-Za-z0-9._:\[\]-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~/-]*)?", control_url):
        raise BundleError("node.env needs a valid private-network AIFLOW4_CONTROL_URL")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", node_name):
        raise BundleError("node.env needs AIFLOW4_NODE_NAME")
    if not re.fullmatch(r"[1-9][0-9]{0,3}", slots):
        raise BundleError("node.env needs a positive AIFLOW4_SLOTS value")
    resolved_token = Path(token_file)
    token_path = resolved_token if resolved_token.is_absolute() else root / resolved_token
    if root != Path("/") and resolved_token.is_absolute():
        token_path = root / resolved_token.relative_to("/")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BundleError("node token file configured by node.env is missing") from exc
    if not re.fullmatch(r"[A-Za-z0-9._~-]{16,512}", token):
        raise BundleError("node token file is empty or malformed")
    return {"control_url": control_url, "node_name": node_name, "token": token}


def _api_state(config: dict[str, str], runner: Callable, work: Path) -> dict[str, Any]:
    state_file = work / "state.json"
    result = runner(["curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                     f'{config["control_url"]}/v1/state', "--output", str(state_file)],
                    capture_output=True, text=True)
    if result.returncode:
        raise BundleError("could not verify node heartbeat with the private control API")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError("control API returned invalid node health state") from exc
    if not next((item for item in state.get("nodes", []) if item.get("name") == config["node_name"]), None):
        raise BundleError("control API has not registered this node")
    return state


def _prepare_assigned_accounts(state: dict[str, Any], node_name: str, root: Path, runner: Callable) -> None:
    """Create worker homes before the next heartbeat lets the agent reconcile assigned accounts."""
    account_root = root / "srv/aiflow/accounts"
    account_root.mkdir(parents=True, exist_ok=True)
    for account in state.get("accounts", []):
        if account.get("node") != node_name:
            continue
        account_id = str(account.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", account_id):
            raise BundleError("control API returned an invalid assigned account id")
        path = account_root / account_id
        if path.is_symlink():
            raise BundleError("assigned account directory cannot be a symlink")
        if not path.exists():
            path.mkdir(mode=0o700)
            runner(["chown", "aiflow-worker:aiflow-worker", str(path)], capture_output=True, text=True)


def install_bundle(bundle: Path, *, root: Path = Path("/"), runner: Callable = _run,
                   wait_seconds: int = 90, poll_seconds: float = 5.0) -> dict[str, Any]:
    """Install with no internet access; ``runner`` and ``root`` are injectable for offline integration tests."""
    bundle = bundle.resolve()
    root = root.resolve()
    manifest = verify_bundle(bundle)
    arch = manifest["architecture"]
    if root == Path("/"):
        actual = _run(["dpkg", "--print-architecture"]).stdout.strip()
        if actual != arch:
            raise BundleError(f"bundle is for {arch}, but this host is {actual}")
        if os.geteuid() != 0:
            raise BundleError("run offline installation as root")
    node_env = root / "etc/aiflow/node.env"
    config = _parse_node_env(node_env, root)
    files = manifest["files"]
    debs = [str(_safe_bundle_path(bundle, item)) for item in files["packages"]]
    if len(debs) != 2:
        raise BundleError("bundle must contain both the node-agent and worker Debian packages")
    dpkg_args = ["dpkg", "-i", *debs] if root == Path("/") else ["dpkg", f"--root={root}", "-i", *debs]
    runner(dpkg_args, capture_output=True, text=True)

    offline_root = root / "opt/aiflow/offline"
    releases = offline_root / "releases"
    payload_source = bundle / "payload"
    payload_target = releases / manifest["release"]
    if payload_target.exists():
        shutil.rmtree(payload_target)
    payload_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(payload_source, payload_target, symlinks=True)
    current = offline_root / "current"
    next_link = offline_root / ".current.new"
    next_link.unlink(missing_ok=True)
    next_link.symlink_to(Path("releases") / manifest["release"])
    os.replace(next_link, current)

    release = _safe_bundle_path(bundle, files["release"])
    release_root = root / "srv/aiflow/releases" / manifest["release"]
    build_root = release_root.with_name(f".{manifest['release']}.offline-new")
    shutil.rmtree(build_root, ignore_errors=True)
    build_root.mkdir(parents=True)
    try:
        with tarfile.open(release, "r:*") as archive:
            _validate_tar_members(archive.getmembers())
            archive.extractall(build_root, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(build_root, ignore_errors=True)
        raise BundleError(f"could not unpack the release archive: {exc}") from exc
    python = offline_root / "current/python/bin/python3.12"
    uv = offline_root / "current/bin/uv"
    wheelhouse = offline_root / "current/wheelhouse"
    app_wheels = sorted(wheelhouse.glob("ai_flow-*.whl")) + sorted(wheelhouse.glob("ai_flow_*.whl"))
    if not app_wheels:
        raise BundleError("offline wheelhouse has no AI Flow application wheel")
    venv = build_root / ".venv"
    runner([str(uv), "venv", "--python", str(python), str(venv)],
           env={**os.environ, "UV_OFFLINE": "1"}, capture_output=True, text=True)
    runner([str(uv), "pip", "install", "--offline", "--no-index", "--find-links", str(wheelhouse),
            "--python", str(venv / "bin/python"), "--requirements",
            str(offline_root / "current/requirements.txt"), str(app_wheels[0])],
           env={**os.environ, "UV_OFFLINE": "1"}, capture_output=True, text=True)
    (build_root / "RELEASE").write_text(manifest["release"] + "\n", encoding="utf-8")
    shutil.rmtree(release_root, ignore_errors=True)
    os.replace(build_root, release_root)

    # Console-script launchers embed the venv path in their shebang. The release directory
    # was staged under a temporary name, so rewrite that prefix before executing any script.
    old_prefix = os.fsencode(str(build_root))
    new_prefix = os.fsencode(str(release_root))
    for launcher in (release_root / ".venv/bin").iterdir():
        if launcher.is_symlink() or not launcher.is_file():
            continue
        contents = launcher.read_bytes()
        if old_prefix in contents:
            launcher.write_bytes(contents.replace(old_prefix, new_prefix))

    site_packages = release_root / ".venv/lib/python3.12/site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    release_path = str(release_root.resolve())
    (site_packages / "aiflow4-release.pth").write_text(
        f"import sys; sys.path.insert(0, {release_path!r})\n", encoding="utf-8")
    launcher = release_root / ".venv/bin/aiflow4"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise BundleError("offline AI Flow installation did not create the aiflow4 launcher")

    image = _safe_bundle_path(bundle, files["image"])
    runner(["docker", "load", "--input", str(image)], capture_output=True, text=True)
    current_link = root / "srv/aiflow/current"
    current_link.parent.mkdir(parents=True, exist_ok=True)
    next_current = current_link.with_name("current.offline-new")
    next_current.unlink(missing_ok=True)
    next_current.symlink_to(Path("releases") / manifest["release"])
    os.replace(next_current, current_link)

    runner([str(current_link / ".venv/bin/aiflow4"), "version"], capture_output=True, text=True)
    runner(["systemctl", "daemon-reload"], capture_output=True, text=True)
    runner(["systemctl", "enable", AGENT_UNIT], capture_output=True, text=True)
    runner(["systemctl", "restart", AGENT_UNIT], capture_output=True, text=True)
    end = time.monotonic() + max(0, wait_seconds)
    last_node: dict[str, Any] | None = None
    while True:
        active = runner(["systemctl", "is-active", "--quiet", AGENT_UNIT], check=False,
                        capture_output=True, text=True).returncode == 0
        if active:
            with tempfile.TemporaryDirectory(prefix="aiflow4-offline-health-") as temporary:
                try:
                    state = _api_state(config, runner, Path(temporary))
                    node = next(item for item in state.get("nodes", []) if item.get("name") == config["node_name"])
                    last_node = node
                    _prepare_assigned_accounts(state, config["node_name"], root, runner)
                    if (node.get("status") == "online" and node.get("release") == manifest["release"]
                            and node.get("missing_tools") == []):
                        if int(node.get("accounts_assigned", 0)):
                            assigned = [str(account.get("id") or "") for account in state.get("accounts", [])
                                        if account.get("node") == config["node_name"]]
                            workers = runner(["systemctl", "list-units", "--state=active", "--plain",
                                              "--no-legend", "aiflow4-worker@*.service"], check=False,
                                             capture_output=True, text=True)
                            active_units = {line.split()[0] for line in workers.stdout.splitlines() if line.split()}
                            if workers.returncode == 0 and any(
                                    f"aiflow4-worker@{account}.service" in active_units for account in assigned):
                                return {"healthy": True, "node": node, "release": manifest["release"]}
                        else:
                            return {"healthy": True, "node": node, "release": manifest["release"]}
                except BundleError:
                    pass
        if time.monotonic() >= end:
            missing = (last_node or {}).get("missing_tools", [])
            raise BundleError("node did not reach online state with green tool preflight and an active worker; "
                              f"missing tools: {', '.join(missing) if missing else 'heartbeat or worker pending'}")
        time.sleep(poll_seconds)


def _extract_bundle(archive_path: Path, destination: Path) -> Path:
    sidecar = archive_path.with_suffix(archive_path.suffix + ".sha256")
    try:
        checksum_fields = sidecar.read_text(encoding="utf-8").strip().split("  ", 1)
    except OSError as exc:
        raise BundleError("bundle archive needs its matching .sha256 sidecar") from exc
    if (len(checksum_fields) != 2 or checksum_fields[1] != archive_path.name
            or not re.fullmatch(r"[0-9a-f]{64}", checksum_fields[0])
            or _sha256(archive_path) != checksum_fields[0]):
        raise BundleError("offline bundle archive checksum verification failed")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            _validate_tar_members(members)
            archive.extractall(destination, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    except (OSError, tarfile.TarError) as exc:
        raise BundleError(f"could not unpack offline bundle: {exc}") from exc
    roots = [path for path in destination.iterdir() if path.is_dir() and (path / "manifest.json").is_file()]
    if len(roots) != 1:
        raise BundleError("offline bundle archive must contain exactly one bundle directory")
    return roots[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="assemble a checksummed bundle on a connected build host")
    build.add_argument("--version", required=True, help="AI Flow release tag, vX.Y.Z")
    build.add_argument("--release", type=Path, required=True, help="signed-release tarball")
    build.add_argument("--image", type=Path, required=True, help="docker save archive for the release image")
    build.add_argument("--image-ref", required=True, help="image tag inside the Docker archive")
    build.add_argument("--versions-lock", type=Path, required=True,
                       help="aiflow-control/versions.lock from the intended control repo revision")
    build.add_argument("--output", type=Path, required=True, help="output offline .tar.gz")
    build.add_argument("--arch", choices=("amd64", "arm64"), help="defaults to the build host architecture")
    build.add_argument("--include-grok", action="store_true", help="also include the optional locked Grok CLI")
    install = sub.add_parser("install", help="install an extracted bundle on a disconnected Ubuntu/Debian node")
    install.add_argument("bundle", type=Path, help="extracted bundle directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(version=args.version, release=args.release, image=args.image,
                                  image_ref=args.image_ref, versions_lock=args.versions_lock,
                                  output=args.output, arch=args.arch, include_grok=args.include_grok)
            print(f"built offline bundle: {result}")
        else:
            bundle = args.bundle
            if bundle.is_file():
                with tempfile.TemporaryDirectory(prefix="aiflow4-offline-unpack-") as temporary:
                    bundle = _extract_bundle(bundle, Path(temporary))
                    result = install_bundle(bundle)
            else:
                result = install_bundle(bundle)
            print(f"installed {result['release']}; node {result['node']['name']} is online with green preflight")
    except BundleError as exc:
        print(f"aiflow4-offline-bundle: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
