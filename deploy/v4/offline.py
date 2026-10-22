"""Build and install target-specific, checksum-verified offline v4 node bundles.

The builder runs on an online machine with the same Ubuntu/Debian release and CPU architecture as the
target. Installation uses only files in the bundle; its sole network calls are to the configured private
control API to inspect and redeem a one-use node join token.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}
SUPPORTED_TARGETS = {
    "ubuntu": {"22.04", "24.04"},
    "debian": {"12"},
}
CLI_PACKAGES = {
    "claude_code": ("@anthropic-ai/claude-code", "claude"),
    "codex": ("@openai/codex", "codex"),
    "opencode": ("opencode-ai", "opencode"),
    "grok": ("@vibe-kit/grok-cli", "grok"),
}
LOCK_ALIASES = {
    "claude_code": ("claude_code", "claude-code", "claude"),
    "codex": ("codex",),
    "opencode": ("opencode",),
    "grok": ("grok",),
    "uv": ("uv",),
    "node": ("node",),
    "python3": ("python3", "python"),
}
VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
# Project stacks whose tools ship inside the bundle (Python and Node.js runtimes). The node agent installs every
# other stack's tools (PHP, Composer, WP-CLI, Playwright) from public registries, so such nodes are refused.
OFFLINE_STACKS = frozenset({"python-3.12", "node-24"})
APT_PACKAGES = ["git", "docker.io", "ca-certificates", "passwd", "util-linux"]
SAFE_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SAFE_ENV_VALUE = re.compile(r"[A-Za-z0-9_./:@\[\]-]+\Z")


class OfflineError(RuntimeError):
    """The bundle is invalid, incompatible or could not be installed."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_versions_lock(path: Path) -> dict[str, str]:
    """Read the project's small tools map using the same YAML parser already used by v4."""
    try:
        import yaml
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report a concise lockfile error to the operator
        raise OfflineError(f"could not read versions.lock ({type(exc).__name__})") from exc
    tools = document.get("tools") if isinstance(document, dict) else None
    if not isinstance(tools, dict):
        raise OfflineError("versions.lock must contain a tools mapping")
    selected: dict[str, str] = {}
    for name, aliases in LOCK_ALIASES.items():
        for alias in aliases:
            value = tools.get(alias)
            if isinstance(value, dict):
                value = value.get("version")
            if value not in (None, ""):
                selected[name] = str(value).strip()
                break
    for required in ("claude_code", "codex", "opencode"):
        value = selected.get(required, "")
        if not VERSION.fullmatch(value):
            raise OfflineError(f"versions.lock must pin {required} to an exact MAJOR.MINOR.PATCH version")
    if selected.get("python3") and not re.fullmatch(r"3\.12(?:\.\d+)?", selected["python3"].removeprefix("v")):
        raise OfflineError("offline node bundles require python3 to be pinned to 3.12")
    return selected


def target_platform(os_release: Path = Path("/etc/os-release"), machine: str | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in os_release.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip().strip('"').strip("'")
    except OSError as exc:
        raise OfflineError("could not identify the target operating system") from exc
    distro = values.get("ID", "").lower()
    version = values.get("VERSION_ID", "")
    architecture = ARCHES.get(machine or platform.machine(), "")
    if version not in SUPPORTED_TARGETS.get(distro, set()):
        raise OfflineError("supported targets are Ubuntu 22.04/24.04 and Debian 12")
    if not architecture:
        raise OfflineError("supported CPU architectures are amd64 and arm64")
    return {"distribution": distro, "version": version, "architecture": architecture}


def _safe_relative(name: str) -> Path:
    posix = PurePosixPath(name)
    if posix.is_absolute() or not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
        raise OfflineError(f"unsafe bundle path: {name}")
    return Path(*posix.parts)


def write_manifest(bundle: Path, *, version: str, target: dict[str, str], versions: dict[str, str],
                   python_executable: str) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in bundle.rglob("*")
                       if (p.is_file() or p.is_symlink())
                       and p.relative_to(bundle).as_posix() not in {"manifest.json", "SHA256SUMS"}):
        rel = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(bundle.resolve()):
                raise OfflineError(f"bundle symlink escapes its root: {rel}")
            files[rel] = {"sha256": sha256(path), "size": path.stat().st_size,
                          "type": "symlink", "target": os.readlink(path)}
        else:
            files[rel] = {"sha256": sha256(path), "size": path.stat().st_size, "type": "file"}
    manifest = {"schema": 1, "version": version, "target": target, "versions": versions,
                "python_executable": python_executable, "files": files}
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sums = [f"{sha256(bundle / name)}  {name}" for name in sorted([*files, "manifest.json"])]
    (bundle / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return manifest


def verify_bundle(bundle: Path) -> dict[str, Any]:
    """Verify every payload byte and reject missing, extra, linked or path-traversing files."""
    try:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OfflineError("bundle manifest is missing or invalid") from exc
    if manifest.get("schema") != 1 or not VERSION.fullmatch(str(manifest.get("version", "")).removeprefix("v")):
        raise OfflineError("unsupported or invalid bundle manifest")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise OfflineError("bundle manifest has no file inventory")
    expected = set(files) | {"manifest.json"}
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*")
              if (p.is_file() or p.is_symlink()) and p.relative_to(bundle).as_posix() != "SHA256SUMS"}
    if actual != expected:
        raise OfflineError("bundle file inventory differs from its manifest")
    for name, detail in files.items():
        rel = _safe_relative(name)
        path = bundle / rel
        if path.is_symlink():
            if detail.get("type") != "symlink" or os.readlink(path) != detail.get("target"):
                raise OfflineError(f"bundle symlink verification failed: {name}")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(bundle.resolve()):
                raise OfflineError(f"bundle symlink escapes its root: {name}")
        elif not path.is_file() or detail.get("type") == "symlink":
            raise OfflineError(f"bundle file type verification failed: {name}")
        if path.stat().st_size != detail.get("size") or sha256(path) != detail.get("sha256"):
            raise OfflineError(f"bundle checksum verification failed: {name}")
    sums: dict[str, str] = {}
    try:
        for line in (bundle / "SHA256SUMS").read_text(encoding="ascii").splitlines():
            digest, separator, name = line.partition("  ")
            if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("bad checksum line")
            sums[name] = digest
    except (OSError, UnicodeError, ValueError) as exc:
        raise OfflineError("bundle SHA256SUMS is missing or invalid") from exc
    sums_expected = expected
    if set(sums) != sums_expected or any(sha256(bundle / _safe_relative(name)) != digest for name, digest in sums.items()):
        raise OfflineError("bundle SHA256SUMS verification failed")
    required = {"release", "image", "wheels", "python", "node", "cli", "packages", "apt", "metadata/versions.lock", "installer"}
    paths = set(files)
    for top in required:
        if not any(name == top or name.startswith(top + "/") for name in paths):
            raise OfflineError(f"bundle is missing required component: {top}")
    return manifest


def safe_extract(archive_path: Path, destination: Path) -> None:
    """Extract an untrusted release tarball without accepting links or path traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                rel = _safe_relative(member.name)
                target = destination / rel
                if not target.resolve().is_relative_to(root):
                    raise OfflineError("release archive contains a path outside its destination")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, member.mode & 0o755)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise OfflineError("release archive contains an unreadable file")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, member.mode & 0o755)
                else:
                    raise OfflineError("release archive contains a link or special file")
    except (tarfile.TarError, OSError) as exc:
        if isinstance(exc, OfflineError):
            raise
        raise OfflineError("release archive is invalid") from exc


def safe_extract_bundle(archive_path: Path, destination: Path) -> None:
    """Extract a bundle archive, allowing only relative symlinks that stay inside its root."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive.getmembers():
                rel = _safe_relative(member.name)
                target = destination / rel
                if not target.resolve().is_relative_to(root):
                    raise OfflineError("bundle archive contains a path outside its destination")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, member.mode & 0o755)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise OfflineError("bundle archive contains an unreadable file")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, member.mode & 0o755)
                elif member.issym():
                    link_target = Path(member.linkname)
                    if link_target.is_absolute() or not (target.parent / link_target).resolve().is_relative_to(root):
                        raise OfflineError("bundle archive contains an unsafe symlink")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                else:
                    raise OfflineError("bundle archive contains a hard link or special file")
    except (tarfile.TarError, OSError) as exc:
        if isinstance(exc, OfflineError):
            raise
        raise OfflineError("offline bundle archive is invalid") from exc


def _run(argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, env=env, cwd=cwd)
    except OSError as exc:
        raise OfflineError(f"could not run {Path(argv[0]).name} ({type(exc).__name__})") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise OfflineError(f"{Path(argv[0]).name} failed: {detail[-1][:240] if detail else result.returncode}")
    return result.stdout.strip()


def _version_matches(actual: str, expected: str) -> bool:
    wanted = expected.removeprefix("v").split(".")
    wanted = wanted[:next((index for index, component in enumerate(wanted) if component.lower() == "x"), len(wanted))]
    if not wanted or any(not component.isdigit() for component in wanted):
        return False
    found = re.search(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){0,2})(?![A-Za-z0-9])", actual)
    if not found:
        return False
    got = found.group(1).removeprefix("v").split(".")
    return len(got) >= len(wanted) and got[:len(wanted)] == wanted


def _download(url: str, destination: Path) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AI-Flow-offline-bundle/1"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            data = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise OfflineError(f"could not download a pinned build artifact ({type(exc).__name__})") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return data


def _fetch(url: str, *, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AI-Flow-offline-bundle/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise OfflineError(f"could not download a pinned build artifact ({type(exc).__name__})") from exc


def _github_asset(repository: str, tag: str, asset_name: str, destination: Path) -> bytes:
    url = f"https://api.github.com/repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "AI-Flow-offline-bundle/1"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            release = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OfflineError(f"could not read pinned release metadata ({type(exc).__name__})") from exc
    asset = next((item for item in release.get("assets", []) if item.get("name") == asset_name), None)
    digest = str((asset or {}).get("digest") or "")
    download_url = str((asset or {}).get("browser_download_url") or "")
    parsed = urllib.parse.urlsplit(download_url)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or parsed.scheme != "https" or parsed.hostname != "github.com":
        raise OfflineError(f"official release has no trusted SHA-256 for {asset_name}")
    data = _download(download_url, destination)
    if hashlib.sha256(data).hexdigest() != digest.removeprefix("sha256:"):
        raise OfflineError(f"checksum verification failed for {asset_name}")
    return data


def _deb_control(deb: Path) -> dict[str, str]:
    """Package, Depends, Pre-Depends and Provides fields of one .deb."""
    fields: dict[str, str] = {}
    for field in ("Package", "Depends", "Pre-Depends", "Provides"):
        fields[field] = _run(["dpkg-deb", "--field", str(deb), field])
    return fields


def verify_apt_closure(apt_debs: list[Path], package_debs: list[Path],
                       control: Callable[[Path], dict[str, str]]) -> None:
    """Every dependency of every bundled .deb must be provided by a bundled .deb or be the AI Flow packages.

    The target has nothing installed except its base image, so the bundle must carry the full closure; a
    missing alternative group is a build error rather than an `apt-get --no-download` failure on the target.
    """
    records = [control(deb) for deb in apt_debs + package_debs]
    provided: set[str] = set()
    for record in records:
        provided.add(record.get("Package", "").strip())
        for item in record.get("Provides", "").split(","):
            name = item.split("(")[0].strip().split(":")[0]
            if name:
                provided.add(name)
    unresolved: set[str] = set()
    for record in records:
        for field in ("Depends", "Pre-Depends"):
            for group in filter(None, (g.strip() for g in record.get(field, "").split(","))):
                names = [alt.split("(")[0].split("[")[0].split("<")[0].strip().split(":")[0]
                         for alt in group.split("|")]
                if not any(name in provided for name in names):
                    unresolved.add(" | ".join(names))
    if unresolved:
        raise OfflineError("apt bundle is not a complete dependency closure for a clean target: "
                           + ", ".join(sorted(unresolved)[:10]))


def _build_debs(packages_dir: Path, version: str) -> None:
    """Build architecture-independent agent and worker packages with their systemd units."""
    deb_version = version.removeprefix("v")
    definitions = {
        "aiflow4-node-agent": ("AI Flow v4 node agent", "systemd, git, docker.io, ca-certificates, passwd, util-linux", "aiflow4-node-agent.service"),
        "aiflow4-worker": ("AI Flow v4 worker template", "aiflow4-node-agent", "aiflow4-worker@.service"),
    }
    units = ROOT / "deploy/v4/node/systemd"
    for name, (description, depends, unit) in definitions.items():
        tree = packages_dir / f"{name}.root"
        unit_path = tree / "lib/systemd/system" / unit
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(units / unit, unit_path)
        control = tree / "DEBIAN/control"
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(
            f"Package: {name}\nVersion: {deb_version}\nSection: admin\nPriority: optional\n"
            f"Architecture: all\nDepends: {depends}\nMaintainer: AI Flow <packages@jooservices.com>\n"
            f"Description: {description}\n", encoding="utf-8")
        postinst = tree / "DEBIAN/postinst"
        postinst.write_text("""#!/bin/sh
set -e
if ! id aiflow-worker >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /srv/aiflow --shell /usr/sbin/nologin aiflow-worker
fi
install -d -o aiflow-worker -g aiflow-worker /srv/aiflow/accounts /srv/aiflow/cache/ai/tmp /srv/aiflow/work
install -d -o root -g root -m 0700 /srv/aiflow/state
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl enable aiflow4-node-agent.service >/dev/null 2>&1 || true
fi
exit 0
""", encoding="utf-8")
        postinst.chmod(0o755)
        output = packages_dir / f"{name}_{deb_version}_all.deb"
        _run(["dpkg-deb", "--build", "--root-owner-group", str(tree), str(output)])
        shutil.rmtree(tree)


def _build_bundle(args: argparse.Namespace) -> Path:
    target = target_platform()
    if os.geteuid() != 0:
        raise OfflineError("build the offline bundle as root so apt can resolve a complete local dependency closure")
    version = args.version
    if not VERSION.fullmatch(version.removeprefix("v")):
        raise OfflineError("release version must be vX.Y.Z")
    version = "v" + version.removeprefix("v")
    lock_path = args.versions_lock.resolve()
    versions = read_versions_lock(lock_path)
    if args.include_grok and not VERSION.fullmatch(versions.get("grok", "")):
        raise OfflineError("--include-grok requires an exact Grok version in versions.lock")
    if args.architecture and args.architecture != target["architecture"]:
        raise OfflineError("build on a machine matching --architecture; node runtimes and wheels are native artifacts")
    if args.distribution and args.distribution != target["distribution"]:
        raise OfflineError("build on the requested distribution; apt package closure is distribution-specific")
    if args.os_version and args.os_version != target["version"]:
        raise OfflineError("build on the requested OS version; apt package closure is OS-specific")
    release_tar = args.release.resolve()
    if not release_tar.is_file():
        raise OfflineError("release tarball does not exist")
    if release_tar.name != f"aiflow4-{version}.tar.gz":
        raise OfflineError("release tarball filename must match --version")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bundle_name = f"aiflow4-offline-{version}-{target['distribution']}-{target['version']}-{target['architecture']}"
    bundle = output / bundle_name
    if bundle.exists():
        shutil.rmtree(bundle)
    for sub in ("release", "image", "wheels", "python", "node", "cli", "packages", "apt", "metadata", "installer"):
        (bundle / sub).mkdir(parents=True, exist_ok=True)

    shutil.copy2(release_tar, bundle / "release" / f"aiflow4-{version}.tar.gz")
    shutil.copy2(lock_path, bundle / "metadata/versions.lock")
    shutil.copy2(ROOT / "deploy/v4/offline.py", bundle / "installer/offline.py")
    installer_entry = bundle / "installer/aiflow4-offline"
    installer_entry.write_text("#!/usr/bin/env python3\nfrom offline import main\nraise SystemExit(main())\n", encoding="utf-8")
    installer_entry.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix="aiflow4-offline-src-") as temp:
        source = Path(temp) / "src"
        safe_extract(release_tar, source)
        if not (source / "pyproject.toml").is_file() or not (source / "uv.lock").is_file():
            raise OfflineError("release archive must contain pyproject.toml and uv.lock")
        uv_path = shutil.which("uv")
        if not uv_path:
            raise OfflineError("uv must be installed on the online bundle builder")
        uv_actual = _run([uv_path, "--version"])
        uv_pin = versions.get("uv", "")
        if uv_pin and not _version_matches(uv_actual, uv_pin):
            raise OfflineError("bundle builder uv does not match versions.lock")
        node_actual = _run(["node", "--version"])
        node_version = node_actual.removeprefix("v")
        node_pin = versions.get("node", "")
        if node_pin and not _version_matches(node_actual, node_pin):
            raise OfflineError("bundle builder Node.js does not match versions.lock")
        node_arch = "x64" if target["architecture"] == "amd64" else "arm64"
        node_file = f"node-v{node_version}-linux-{node_arch}.tar.xz"
        sums_data = _fetch("https://nodejs.org/dist/v" + node_version + "/SHASUMS256.txt").decode()
        node_hash = next((line.split()[0] for line in sums_data.splitlines()
                          if line.split()[-1:] == [node_file]), "")
        if not re.fullmatch(r"[0-9a-f]{64}", node_hash):
            raise OfflineError("Node.js release has no official checksum for this architecture")
        node_archive = bundle / "node" / node_file
        node_data = _download(f"https://nodejs.org/dist/v{node_version}/{node_file}", node_archive)
        if hashlib.sha256(node_data).hexdigest() != node_hash:
            raise OfflineError("Node.js runtime checksum verification failed")
        safe_extract_bundle(node_archive, bundle / "node")
        node_root = bundle / "node" / f"node-v{node_version}-linux-{node_arch}"
        node_bin = node_root / "bin"
        npm = node_bin / "npm"
        cli_root = bundle / "cli"
        cli_names = ["claude_code", "codex", "opencode"] + (["grok"] if args.include_grok else [])
        specs = [f"{CLI_PACKAGES[key][0]}@{versions[key]}" for key in cli_names]
        _run([str(npm), "install", "--prefix", str(cli_root), "--no-audit", "--no-fund", "--omit=dev",
              "--package-lock=false", *specs], env={**os.environ, "PATH": f"{node_bin}:{os.environ.get('PATH', '')}"})
        for key in cli_names:
            package, binary = CLI_PACKAGES[key]
            package_json = cli_root / "node_modules" / package / "package.json"
            try:
                actual = json.loads(package_json.read_text(encoding="utf-8"))["version"]
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                raise OfflineError(f"npm did not install the locked {key} package") from exc
            if actual != versions[key] or not (cli_root / "node_modules/.bin" / binary).exists():
                raise OfflineError(f"installed {key} does not match versions.lock")
        codex_host_arch = "x86_64" if target["architecture"] == "amd64" else "aarch64"
        host_asset = f"codex-code-mode-host-{codex_host_arch}-unknown-linux-musl.tar.gz"
        host_archive = bundle / "cli" / host_asset
        host_data = _github_asset("openai/codex", f"rust-v{versions['codex']}", host_asset, host_archive)
        import io
        with tarfile.open(fileobj=io.BytesIO(host_data), mode="r:gz") as archive:
            matches = [member for member in archive.getmembers()
                       if member.isfile() and PurePosixPath(member.name).name == "codex-code-mode-host"]
            if len(matches) != 1:
                raise OfflineError("pinned Codex release is missing codex-code-mode-host")
            source_binary = archive.extractfile(matches[0])
            if source_binary is None:
                raise OfflineError("could not read pinned Codex host binary")
            codex_entry = cli_root / "node_modules/.bin/codex"
            if not codex_entry.is_file():
                raise OfflineError("installed Codex package is missing its executable")
            host_directory = codex_entry.resolve().parent
            host_binary = host_directory / "codex-code-mode-host"
            host_binary.parent.mkdir(parents=True, exist_ok=True)
            host_binary.write_bytes(source_binary.read())
            host_binary.chmod(0o755)
        (host_binary.parent / "codex-code-mode-host.version").write_text(versions["codex"] + "\n", encoding="utf-8")

        py_root = bundle / "python/runtime"
        py_root.mkdir(parents=True, exist_ok=True)
        py_env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(py_root), "UV_MANAGED_PYTHON": "1"}
        _run([uv_path, "python", "install", "3.12"], env=py_env)
        python_path = Path(_run([uv_path, "python", "find", "3.12"], env=py_env)).resolve()
        python_executable = python_path.relative_to(bundle / "python").as_posix()
        uv_copy = bundle / "python/uv"
        shutil.copy2(uv_path, uv_copy)
        uv_copy.chmod(0o755)
        requirements = Path(temp) / "requirements.txt"
        _run([uv_path, "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
              "--output-file", str(requirements)], cwd=source)
        pip_env = Path(temp) / "pip-env"
        _run([uv_path, "venv", "--seed", "--python", str(python_path), str(pip_env)])
        _run([str(pip_env / "bin/python"), "-m", "pip", "download", "--only-binary=:all:",
              "--requirement", str(requirements), "--dest", str(bundle / "wheels")])
        _run([uv_path, "build", "--wheel", "--out-dir", str(bundle / "wheels")], cwd=source)
        wheel_names = list((bundle / "wheels").glob("ai_flow-*.whl"))
        if len(wheel_names) != 1:
            raise OfflineError("release build did not produce exactly one AI Flow wheel")

    image_ref = args.image or f"ghcr.io/jooservices/ai-flow/control-api:{version}"
    image_arch = target["architecture"]
    with tempfile.TemporaryDirectory(prefix="aiflow4-offline-image-") as temp:
        source = Path(temp) / "src"
        safe_extract(release_tar, source)
        _run(["docker", "build", "--platform", f"linux/{image_arch}",
              "--build-arg", f"AIFLOW4_VERSION={version}",
              "--build-arg", "AIFLOW4_REVISION=offline-bundle",
              "--file", str(source / "deploy/v4/control/Dockerfile"), "--tag", image_ref, str(source)])
    inspect = _run(["docker", "image", "inspect", image_ref, "--format", "{{.Os}}/{{.Architecture}} {{index .Config.Labels \"org.opencontainers.image.version\"}}"])
    if inspect != f"linux/{'amd64' if image_arch == 'amd64' else 'arm64'} {version}":
        raise OfflineError("control image platform or version label does not match the bundle")
    _run(["docker", "save", "--output", str(bundle / "image/control-api.tar"), image_ref])

    _build_debs(bundle / "packages", version)
    apt_cache = bundle / "apt"
    (apt_cache / "partial").mkdir(parents=True, exist_ok=True)
    _run(["apt-get", "update", "-qq"])
    # Resolve against an empty dpkg status so packages already installed on this builder are still downloaded:
    # a clean target has none of them. The closure is then checked from the .deb control data.
    with tempfile.TemporaryDirectory(prefix="aiflow4-offline-apt-") as clean:
        empty_status = Path(clean) / "status"
        empty_status.write_text("", encoding="utf-8")
        _run(["apt-get", "--download-only", "--yes", "--no-install-recommends",
              "-o", f"Dir::Cache::archives={apt_cache}", "-o", f"Dir::State::status={empty_status}",
              "-o", "APT::Get::Assume-Yes=true", "install", *APT_PACKAGES])
    verify_apt_closure(sorted(apt_cache.glob("*.deb")), sorted((bundle / "packages").glob("*.deb")), _deb_control)
    python_version = _run([str(python_path), "--version"]).split()[-1]
    write_manifest(bundle, version=version, target=target,
                   versions={**versions, "uv": uv_actual.split()[-1], "node": node_version,
                             "python": python_version, "image": image_ref},
                   python_executable=python_executable)
    archive = output / f"{bundle_name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle, arcname=bundle_name)
    return archive


class OfflineInstaller:
    """Install bundle contents without a package registry, image registry or public internet access."""

    def __init__(self, bundle: Path, *, root: Path = Path("/"), target: dict[str, str] | None = None,
                 runner: Callable[..., Any] | None = None,
                 post_json: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
                 get_json: Callable[[str, str, str], dict[str, Any]] | None = None):
        self.bundle = bundle.resolve()
        self.root = root
        self.target = target or target_platform()
        self.runner = runner or subprocess.run
        self.post_json = post_json or _post_json
        self.get_json = get_json or _get_json
        self.manifest: dict[str, Any] = {}

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    def run(self, argv: list[str], *, check: bool = True) -> str:
        result = self.runner(argv, capture_output=True, text=True, check=False)
        if check and result.returncode:
            error = (result.stderr or result.stdout or "").strip().splitlines()
            raise OfflineError(f"{Path(argv[0]).name} failed: {error[-1][:240] if error else result.returncode}")
        return (result.stdout or "").strip()

    def _copy_tree(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)

    def install(self, *, control_url: str, join_token_file: Path, slots: int = 2,
                wait_seconds: int = 90, health_wait_seconds: int = 600) -> str:
        self.manifest = verify_bundle(self.bundle)
        if self.manifest["target"] != self.target:
            raise OfflineError("bundle target does not match this operating system and architecture")
        if os.geteuid() != 0 and self.root == Path("/"):
            raise OfflineError("run offline installation as root")
        version = self.manifest["version"]
        if not 1 <= slots <= 1024:
            raise OfflineError("slots must be between 1 and 1024")
        parsed = urllib.parse.urlsplit(control_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise OfflineError("control URL must be an HTTP(S) URL without embedded credentials")
        try:
            join_token = join_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise OfflineError("join token file is not readable") from exc
        if not join_token or "\n" in join_token or "\r" in join_token:
            raise OfflineError("join token file is empty or invalid")

        apt_debs = sorted((self.bundle / "apt").glob("*.deb"))
        if not apt_debs:
            raise OfflineError("bundle has no offline OS dependency packages")
        package_debs = sorted((self.bundle / "packages").glob("*.deb"))
        if {p.name.split("_", 1)[0] for p in package_debs} != {"aiflow4-node-agent", "aiflow4-worker"}:
            raise OfflineError("bundle must contain agent and worker Debian packages")
        self.run(["apt-get", "--no-download", "--yes", "install", *[str(p) for p in apt_debs + package_debs]])
        image_archive = self.bundle / "image/control-api.tar"
        self.run(["docker", "load", "--input", str(image_archive)])

        py_dir = self.path("/opt/aiflow/python")
        self._copy_tree(self.bundle / "python/runtime", py_dir)
        python_exec = self.path("/opt/aiflow/python") / self.manifest["python_executable"].removeprefix("runtime/")
        uv_path = self.path("/usr/local/bin/uv")
        uv_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.bundle / "python/uv", uv_path)
        uv_path.chmod(0o755)
        python_bin = self.path("/usr/local/bin")
        for name in ("python3", "python3.12"):
            link = python_bin / name
            link.unlink(missing_ok=True)
            link.symlink_to(python_exec)

        node_source = next((p for p in (self.bundle / "node").glob("node-v*-linux-*/bin") if p.is_dir()), None)
        if not node_source:
            raise OfflineError("bundle has no Node.js runtime")
        node_destination = self.path("/opt/aiflow/node")
        self._copy_tree(node_source.parent, node_destination)
        cli_destination = self.path("/opt/aiflow/cli")
        self._copy_tree(self.bundle / "cli/node_modules", cli_destination / "node_modules")
        local_bin = self.path("/usr/local/bin")
        local_bin.mkdir(parents=True, exist_ok=True)
        for name in ("node", "npm", "npx"):
            source = node_destination / "bin" / name
            if source.exists():
                (local_bin / name).unlink(missing_ok=True)
                (local_bin / name).symlink_to(source)
        for _, binary in CLI_PACKAGES.values():
            source = cli_destination / "node_modules/.bin" / binary
            if source.exists():
                (local_bin / binary).unlink(missing_ok=True)
                (local_bin / binary).symlink_to(source)
        codex_entry = self.bundle / "cli/node_modules/.bin/codex"
        if not codex_entry.is_file():
            raise OfflineError("bundle is missing the Codex executable link")
        codex_relative = codex_entry.resolve().relative_to(self.bundle / "cli/node_modules")
        codex_installed = cli_destination / "node_modules" / codex_relative
        host_source = codex_installed.parent / "codex-code-mode-host"
        if not host_source.is_file():
            raise OfflineError("bundle is missing the host binary beside Codex")
        (local_bin / "codex-code-mode-host").unlink(missing_ok=True)
        (local_bin / "codex-code-mode-host").symlink_to(host_source)
        (local_bin / "codex-code-mode-host.version").unlink(missing_ok=True)
        (local_bin / "codex-code-mode-host.version").symlink_to(host_source.with_name("codex-code-mode-host.version"))

        release_archive = next((self.bundle / "release").glob("aiflow4-*.tar.gz"), None)
        if not release_archive:
            raise OfflineError("bundle has no AI Flow release archive")
        releases = self.path("/srv/aiflow/releases")
        release_dir = releases / version
        releases.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=releases))
        try:
            safe_extract(release_archive, stage)
            if not (stage / "pyproject.toml").is_file() or not (stage / "deploy/v4/node/systemd/aiflow4-node-agent.service").is_file():
                raise OfflineError("release archive is missing node runtime files")
            python_venv = stage / ".venv"
            self.run([str(uv_path), "venv", "--seed", "--python", str(python_exec), str(python_venv)])
            wheels = self.bundle / "wheels"
            project_wheels = list(wheels.glob("ai_flow-*.whl"))
            if len(project_wheels) != 1:
                raise OfflineError("bundle must contain exactly one AI Flow wheel")
            self.run([str(uv_path), "pip", "install", "--offline", "--no-index", "--find-links", str(wheels),
                      "--python", str(python_venv / "bin/python"), str(project_wheels[0])])
            (stage / "RELEASE").write_text(version + "\n", encoding="utf-8")
            if release_dir.exists():
                shutil.rmtree(release_dir)
            releases.mkdir(parents=True, exist_ok=True)
            os.replace(stage, release_dir)
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

        current = self.path("/srv/aiflow/current")
        current.parent.mkdir(parents=True, exist_ok=True)
        current_new = current.with_name("current.new")
        current_new.unlink(missing_ok=True)
        current_new.symlink_to(release_dir)
        current_new.replace(current)
        installer_source = release_dir / "deploy/v4/bin/aiflow4-node-install"
        installer_target = self.path("/usr/local/sbin/aiflow4-node-install")
        installer_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(installer_source, installer_target)
        installer_target.chmod(0o755)

        base_url = control_url.rstrip("/")
        inspected = self.post_json(base_url, "/v1/nodes/join-tokens/inspect", {"join_token": join_token})
        node_name = str(inspected.get("name", ""))
        if not SAFE_NODE.fullmatch(node_name):
            raise OfflineError("control API returned an invalid node name")
        if inspected.get("release") != version:
            raise OfflineError("join grant does not target the release included in this offline bundle")
        stacks = inspected.get("stacks", inspected.get("stack", []))
        stacks = [stacks] if isinstance(stacks, str) and stacks else stacks
        if not isinstance(stacks, list) or any(not isinstance(s, str) for s in stacks):
            raise OfflineError("control API returned an invalid project stack list")
        unsupported = sorted(set(stacks) - OFFLINE_STACKS)
        if unsupported:
            # Checked before joining so the single-use token is not consumed by a node that could never go green.
            raise OfflineError("the offline bundle cannot provide the tools of project stack(s) "
                               + ", ".join(unsupported) + "; use an online install for this project")
        joined = self.post_json(base_url, "/v1/nodes/join", {"join_token": join_token})
        if joined.get("node") != node_name or joined.get("release") != version:
            raise OfflineError("join response does not match the node grant and bundled release")
        token = str(joined.get("node_token", ""))
        if not token or "\n" in token or "\r" in token:
            raise OfflineError("control API returned an invalid node token")
        accounts = joined.get("accounts", [])
        if not isinstance(accounts, list):
            raise OfflineError("control API returned an invalid account list")
        state_dir = self.path("/srv/aiflow/state")
        state_dir.mkdir(parents=True, exist_ok=True)
        token_file = state_dir / "node.token"
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        etc_dir = self.path("/etc/aiflow")
        etc_dir.mkdir(parents=True, exist_ok=True)
        env_values = {
            "AIFLOW4_CONTROL_URL": base_url,
            "AIFLOW4_NODE_NAME": node_name,
            "AIFLOW4_NODE_TOKEN_FILE": "/srv/aiflow/state/node.token",
            "AIFLOW4_SLOTS": str(slots),
            "AIFLOW4_POLICY_RELAY": "1",
            "AIFLOW4_SELF_UPDATE": "1",
        }
        for value in env_values.values():
            if not SAFE_ENV_VALUE.fullmatch(value):
                raise OfflineError("control API returned a value unsafe for the systemd environment file")
        env_file = etc_dir / "node.env"
        env_file.write_text("".join(f'{key}="{value}"\n' for key, value in env_values.items()), encoding="utf-8")
        env_file.chmod(0o600)
        accounts_root = self.path("/srv/aiflow/accounts")
        accounts_root.mkdir(parents=True, exist_ok=True)
        for account in accounts:
            account = str(account)
            if not SAFE_NODE.fullmatch(account):
                raise OfflineError("control API returned an invalid account id")
            account_path = accounts_root / account
            account_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root == Path("/"):
                try:
                    worker = pwd.getpwnam("aiflow-worker")
                    os.chown(account_path, worker.pw_uid, worker.pw_gid)
                except KeyError as exc:
                    raise OfflineError("Debian packages did not create aiflow-worker") from exc
        if self.root != Path("/"):
            for path in (accounts_root, self.path("/srv/aiflow/cache/ai/tmp"), self.path("/srv/aiflow/work")):
                path.mkdir(parents=True, exist_ok=True)

        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable", "--now", "aiflow4-node-agent.service"])
        self.run([str(current / ".venv/bin/aiflow4"), "version"])
        active = False
        for _ in range(max(1, wait_seconds // 2)):
            result = self.runner(["systemctl", "is-active", "--quiet", "aiflow4-node-agent.service"],
                                 capture_output=True, text=True, check=False)
            worker_units = [f"aiflow4-worker@{account}.service" for account in accounts]
            workers_active = all(
                self.runner(["systemctl", "is-active", "--quiet", unit],
                            capture_output=True, text=True, check=False).returncode == 0
                for unit in worker_units)
            if result.returncode == 0 and workers_active:
                active = True
                break
            time_sleep(2)
        if not active:
            raise OfflineError("aiflow4-node-agent or an assigned worker service did not become active")
        # Active units are not enough: the agent must have heartbeated and the project preflight must be green.
        health: dict[str, Any] = {}
        for _ in range(max(1, health_wait_seconds // 2)):
            try:
                health = self.get_json(base_url, f"/v1/nodes/{node_name}/health", token)
            except OfflineError:
                health = {}
            age = health.get("heartbeat_age_seconds")
            if (health.get("status") == "online" and isinstance(age, (int, float)) and age <= 60
                    and health.get("preflight_ok") is True):
                return node_name
            time_sleep(2)
        age = health.get("heartbeat_age_seconds")
        if health.get("status") != "online" or not isinstance(age, (int, float)) or age > 60:
            raise OfflineError("node agent is running but has not reported a healthy heartbeat to the control API")
        missing = ", ".join(str(t) for t in health.get("missing_tools", [])) or "unknown"
        raise OfflineError(f"node is online but project preflight is not green; missing or mismatched tools: {missing}")


def _get_json(base_url: str, path: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(base_url + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OfflineError(f"could not read node health ({type(exc).__name__})") from exc
    if not isinstance(result, dict):
        raise OfflineError("control API returned an invalid node health response")
    return result


def time_sleep(seconds: int) -> None:
    import time
    time.sleep(seconds)


def _post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(base_url + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OfflineError(f"could not reach the configured control API ({type(exc).__name__})") from exc
    if not isinstance(result, dict):
        raise OfflineError("control API returned an invalid node enrollment response")
    return result


def _extract_bundle_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    safe_extract_bundle(archive, destination)
    roots = [p for p in destination.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise OfflineError("offline bundle archive must have one top-level directory")
    return roots[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build, verify and install an AI Flow v4 offline node bundle")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="assemble a target-specific bundle on an online Ubuntu/Debian host")
    build.add_argument("--version", required=True, help="vX.Y.Z release tag")
    build.add_argument("--release", type=Path, required=True, help="matching aiflow4-vX.Y.Z.tar.gz")
    build.add_argument("--versions-lock", type=Path, required=True, help="aiflow-control/versions.lock")
    build.add_argument("--image", help="matching control-api image ref (default: GHCR release tag)")
    build.add_argument("--output", type=Path, default=Path("dist"))
    build.add_argument("--distribution", choices=("ubuntu", "debian"))
    build.add_argument("--os-version", help="22.04, 24.04 or Debian 12")
    build.add_argument("--architecture", choices=("amd64", "arm64"))
    build.add_argument("--include-grok", action="store_true", help="also include the optional locked Grok CLI")
    verify = sub.add_parser("verify", help="verify all files in an extracted bundle")
    verify.add_argument("bundle", type=Path)
    install = sub.add_parser("install", help="install a bundle without public internet access")
    install.add_argument("bundle", type=Path, help="bundle directory or .tar.gz bundle")
    install.add_argument("--control-url", required=True, help="private control API address reachable by this node")
    install.add_argument("--join-token-file", required=True, type=Path, help="one-use node join token file")
    install.add_argument("--slots", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            archive = _build_bundle(args)
            print(archive)
        elif args.command == "verify":
            manifest = verify_bundle(args.bundle)
            print(f"verified {manifest['version']} for {manifest['target']['distribution']} {manifest['target']['version']} {manifest['target']['architecture']}")
        else:
            bundle = args.bundle
            temp_context = None
            if bundle.is_file():
                temp_context = tempfile.TemporaryDirectory(prefix="aiflow4-offline-")
                bundle = _extract_bundle_archive(bundle, Path(temp_context.name))
            node = OfflineInstaller(bundle).install(control_url=args.control_url,
                                                   join_token_file=args.join_token_file, slots=args.slots)
            if temp_context:
                temp_context.cleanup()
            print(f"installed {node} from {bundle}")
        return 0
    except OfflineError as exc:
        print(f"aiflow4-offline: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
