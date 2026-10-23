"""Offline bundle inventory, Debian package contents and network-free node installation."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import ssl
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.v4 import offline


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    for part in ("release", "image", "wheels", "python/runtime/cpython-3.12.3/bin", "node/node-v24.4.0-linux-x64/bin",
                 "node/node-v24.4.0-linux-x64/lib/node_modules/npm/bin", "cli/node_modules/.bin",
                 "cli/node_modules/@anthropic-ai/claude-code", "cli/node_modules/@openai/codex",
                 "cli/node_modules/opencode-ai", "cli/bin", "packages", "apt", "metadata", "installer"):
        (bundle / part).mkdir(parents=True, exist_ok=True)
    release_source = tmp_path / "release"
    (release_source / "deploy/v4/node/systemd").mkdir(parents=True)
    (release_source / "deploy/v4/bin").mkdir(parents=True)
    (release_source / "pyproject.toml").write_text("[project]\nname = 'ai-flow'\n", encoding="utf-8")
    (release_source / "deploy/v4/node/systemd/aiflow4-node-agent.service").write_text("[Unit]\n", encoding="utf-8")
    installer = release_source / "deploy/v4/bin/aiflow4-node-install"
    installer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installer.chmod(0o755)
    with tarfile.open(bundle / "release/aiflow4-v4.1.0.tar.gz", "w:gz") as archive:
        archive.add(release_source / "pyproject.toml", arcname="pyproject.toml")
        archive.add(release_source / "deploy", arcname="deploy")

    (bundle / "image/control-api.tar").write_bytes(b"docker image archive")
    (bundle / "image/services.tar").write_bytes(b"postgres and redis images")
    (bundle / "wheels/ai_flow-4.1.0-py3-none-any.whl").write_bytes(b"wheel")
    bundled_python = bundle / "python/runtime/cpython-3.12.3/bin/python3.12"
    bundled_python.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n', encoding="utf-8")
    bundled_python.chmod(0o755)
    (bundle / "python/uv").write_text("uv", encoding="utf-8")
    node = bundle / "node/node-v24.4.0-linux-x64"
    (node / "bin/node").write_text("node", encoding="utf-8")
    (node / "lib/node_modules/npm/bin/npm-cli.js").write_text("npm", encoding="utf-8")
    (node / "lib/node_modules/npm/bin/npx-cli.js").write_text("npx", encoding="utf-8")
    (node / "bin/npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
    (node / "bin/npx").symlink_to("../lib/node_modules/npm/bin/npx-cli.js")
    cli = bundle / "cli/node_modules"
    for package, binary in (("@anthropic-ai/claude-code", "claude"), ("@openai/codex", "codex"),
                            ("opencode-ai", "opencode")):
        (cli / package / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        (cli / ".bin" / binary).symlink_to("../" + package + "/cli.js")
    (cli / "@openai/codex/codex-code-mode-host").write_text("host", encoding="utf-8")
    (cli / "@openai/codex/codex-code-mode-host.version").write_text("0.147.0\n", encoding="utf-8")
    (bundle / "packages/aiflow4-node-agent_4.1.0_all.deb").write_bytes(b"agent package")
    (bundle / "packages/aiflow4-worker_4.1.0_all.deb").write_bytes(b"worker package")
    (bundle / "apt/git_2.deb").write_bytes(b"git package and local dependencies")
    (bundle / "metadata/versions.lock").write_text(
        "schema: 1\ntools:\n  claude_code: 2.1.305\n  codex: 0.147.0\n  opencode: 1.2.3\n", encoding="utf-8")
    (bundle / "installer/offline.py").write_text("# bundled installer module\n", encoding="utf-8")
    (bundle / "installer/aiflow4-offline").write_text(offline.INSTALLER_BOOTSTRAP, encoding="utf-8")
    (bundle / "installer/aiflow4-offline").chmod(0o755)
    offline.write_manifest(bundle, version="v4.1.0",
                           target={"distribution": "ubuntu", "version": "24.04", "architecture": "amd64"},
                           versions={"claude_code": "2.1.305", "codex": "0.147.0", "opencode": "1.2.3",
                                     "uv": "0.13.4", "node": "24.4.0", "python": "3.12.3"},
                           python_executable="runtime/cpython-3.12.3/bin/python3.12")
    return bundle


def test_versions_lock_requires_exact_provider_cli_pins_and_grok_is_optional(tmp_path):
    path = tmp_path / "versions.lock"
    path.write_text("schema: 1\ntools:\n  claude_code: {version: 2.1.305}\n  codex: 0.147.0\n  opencode: 1.2.3\n", encoding="utf-8")
    assert offline.read_versions_lock(path) == {
        "claude_code": "2.1.305", "codex": "0.147.0", "opencode": "1.2.3"}
    path.write_text("schema: 1\ntools:\n  claude_code: 2.1\n  codex: 0.147.0\n  opencode: 1.2.3\n", encoding="utf-8")
    with pytest.raises(offline.OfflineError, match="exact"):
        offline.read_versions_lock(path)


@pytest.mark.parametrize(("distro", "version", "machine", "expected"), [
    ("ubuntu", "22.04", "x86_64", "amd64"),
    ("ubuntu", "24.04", "aarch64", "arm64"),
    ("debian", "12", "x86_64", "amd64"),
])
def test_supported_bundle_targets_cover_ubuntu_and_debian_matrix(tmp_path, distro, version, machine, expected):
    release = tmp_path / "os-release"
    release.write_text(f'ID={distro}\nVERSION_ID="{version}"\n', encoding="utf-8")
    assert offline.target_platform(release, machine) == {
        "distribution": distro, "version": version, "architecture": expected}


def test_bundle_builder_assembles_locked_tools_runtime_image_wheels_and_checksums(tmp_path, monkeypatch):
    release_source = tmp_path / "source"
    (release_source / "deploy/v4/control").mkdir(parents=True)
    (release_source / "pyproject.toml").write_text("[project]\nname='ai-flow'\n", encoding="utf-8")
    (release_source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    release_tar = tmp_path / "aiflow4-v4.1.0.tar.gz"
    with tarfile.open(release_tar, "w:gz") as archive:
        archive.add(release_source / "pyproject.toml", arcname="pyproject.toml")
        archive.add(release_source / "uv.lock", arcname="uv.lock")
        archive.add(release_source / "deploy", arcname="deploy")
    lock = tmp_path / "versions.lock"
    lock.write_text(
        "schema: 1\ntools:\n  claude_code: 2.1.305\n  codex: 0.147.0\n  opencode: 1.2.3\n"
        "  node: 24.x\n  uv: 0.13.4\n", encoding="utf-8")
    target = {"distribution": "ubuntu", "version": "24.04", "architecture": "amd64"}
    monkeypatch.setattr(offline, "target_platform", lambda: target)
    monkeypatch.setattr(offline.os, "geteuid", lambda: 0)
    monkeypatch.setattr(offline.shutil, "which", lambda command: "/usr/local/bin/uv" if command == "uv" else None)

    node_root = "node-v24.4.0-linux-x64"
    node_archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=node_archive_buffer, mode="w:xz") as archive:
        info = tarfile.TarInfo(f"{node_root}/bin/node")
        info.size = len(b"node")
        archive.addfile(info, io.BytesIO(b"node"))
        info = tarfile.TarInfo(f"{node_root}/bin/npm")
        info.size = len(b"npm")
        archive.addfile(info, io.BytesIO(b"npm"))
    node_archive_data = node_archive_buffer.getvalue()
    node_filename = f"{node_root}.tar.xz"
    monkeypatch.setattr(offline, "_fetch", lambda _url: f"{hashlib.sha256(node_archive_data).hexdigest()}  {node_filename}\n".encode())

    def download(_url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(node_archive_data)
        return node_archive_data

    monkeypatch.setattr(offline, "_download", download)

    host_buffer = io.BytesIO()
    with tarfile.open(fileobj=host_buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("release/bin/codex-code-mode-host")
        info.size = len(b"host")
        archive.addfile(info, io.BytesIO(b"host"))
    host_archive = host_buffer.getvalue()

    def github_asset(_repo, _tag, _name, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(host_archive)
        return host_archive

    monkeypatch.setattr(offline, "_github_asset", github_asset)

    apt_calls: list[list[str]] = []

    def run(argv, *, env=None, cwd=None):
        argv = [str(value) for value in argv]
        name = Path(argv[0]).name
        if name == "uv" and argv[1:] == ["--version"]:
            return "uv 0.13.4"
        if argv[:2] == ["node", "--version"]:
            return "v24.4.0"
        if name == "npm" and argv[1:2] == ["install"]:
            prefix = Path(argv[argv.index("--prefix") + 1]) / "node_modules"
            for spec in argv[argv.index("--package-lock=false") + 1:]:
                package, version = spec.rsplit("@", 1)
                folder = prefix / package
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "cli.js").write_text("cli", encoding="utf-8")
                (folder / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
                binary = {"@anthropic-ai/claude-code": "claude", "@openai/codex": "codex",
                          "opencode-ai": "opencode"}[package]
                binary_dir = prefix / ".bin"
                binary_dir.mkdir(exist_ok=True)
                (binary_dir / binary).symlink_to("../" + package + "/cli.js")
            return ""
        if name == "uv" and argv[1:3] == ["python", "install"]:
            executable = Path(env["UV_PYTHON_INSTALL_DIR"]) / "cpython-3.12.3-linux-x86_64-gnu/bin/python3.12"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("python", encoding="utf-8")
            return ""
        if name == "uv" and argv[1:3] == ["python", "find"]:
            return str(Path(env["UV_PYTHON_INSTALL_DIR"]) / "cpython-3.12.3-linux-x86_64-gnu/bin/python3.12")
        if name == "uv" and argv[1:2] == ["export"]:
            requirements = Path(argv[argv.index("--output-file") + 1])
            requirements.write_text("fastapi==0.115.0\n", encoding="utf-8")
            return ""
        if name == "python" and argv[1:3] == ["-m", "pip"]:
            wheels = Path(argv[argv.index("--dest") + 1])
            (wheels / "fastapi-0.115.0-py3-none-any.whl").write_bytes(b"dependency wheel")
            return ""
        if name == "uv" and argv[1:2] == ["build"]:
            wheels = Path(argv[argv.index("--out-dir") + 1])
            (wheels / "ai_flow-4.1.0-py3-none-any.whl").write_bytes(b"project wheel")
            return ""
        if name == "python3.12" and argv[1:] == ["--version"]:
            return "Python 3.12.3"
        if argv[:2] == ["docker", "image"]:
            return "linux/amd64 v4.1.0"
        if argv[:2] == ["docker", "save"]:
            Path(argv[argv.index("--output") + 1]).write_bytes(b"image archive")
            return ""
        if argv[0] == "apt-get" and "--download-only" in argv:
            apt_calls.append(argv)
            archive_dir = Path(argv[argv.index("-o") + 1].split("=", 1)[1])
            (archive_dir / "git_2.deb").write_bytes(b"apt package")
        return ""

    monkeypatch.setattr(offline, "_run", run)

    def build_debs(packages, _version):
        (packages / "aiflow4-node-agent_4.1.0_all.deb").write_bytes(b"agent")
        (packages / "aiflow4-worker_4.1.0_all.deb").write_bytes(b"worker")

    monkeypatch.setattr(offline, "_build_debs", build_debs)
    output = tmp_path / "output"
    args = SimpleNamespace(version="v4.1.0", versions_lock=lock, release=release_tar, output=output,
                           architecture=None, distribution=None, os_version=None, include_grok=False, image=None)
    archive = offline._build_bundle(args)
    extracted = offline._extract_bundle_archive(archive, tmp_path / "extracted")
    manifest = offline.verify_bundle(extracted)
    assert manifest["versions"]["claude_code"] == "2.1.305"
    assert manifest["versions"]["codex"] == "0.147.0"
    assert manifest["versions"]["opencode"] == "1.2.3"
    assert "cli/node_modules/@openai/codex/codex-code-mode-host" in manifest["files"]
    assert "image/control-api.tar" in manifest["files"]
    assert "wheels/fastapi-0.115.0-py3-none-any.whl" in manifest["files"]
    assert "wheels/ai_flow-4.1.0-py3-none-any.whl" in manifest["files"]
    # apt resolves against an empty dpkg status (no builder-installed package is skipped), never --reinstall
    assert len(apt_calls) == 1 and "--reinstall" not in apt_calls[0]
    assert any(a.startswith("Dir::State::status=") for a in apt_calls[0])


def test_bundle_checksum_manifest_covers_release_image_wheels_and_pinned_cli_payloads(tmp_path):
    bundle = _bundle(tmp_path)
    manifest = offline.verify_bundle(bundle)
    assert manifest["versions"]["claude_code"] == "2.1.305"
    assert manifest["versions"]["codex"] == "0.147.0"
    assert manifest["versions"]["opencode"] == "1.2.3"
    assert {"release/aiflow4-v4.1.0.tar.gz", "image/control-api.tar",
            "wheels/ai_flow-4.1.0-py3-none-any.whl",
            "cli/node_modules/@openai/codex/codex-code-mode-host"} <= set(manifest["files"])
    (bundle / "image/control-api.tar").write_bytes(b"changed after bundle creation")
    with pytest.raises(offline.OfflineError, match="checksum"):
        offline.verify_bundle(bundle)


def test_bundle_archive_extracts_checked_internal_node_and_cli_symlinks(tmp_path):
    bundle = _bundle(tmp_path)
    archive_path = tmp_path / "offline.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(bundle, arcname="bundle")
    extracted = offline._extract_bundle_archive(archive_path, tmp_path / "extract")
    manifest = offline.verify_bundle(extracted)
    assert manifest["version"] == "v4.1.0"
    assert (extracted / "node/node-v24.4.0-linux-x64/bin/npm").is_symlink()


def test_installer_bootstraps_from_bundled_python_without_host_python(tmp_path):
    bundle = _bundle(tmp_path)
    host_tools = tmp_path / "host-tools"
    host_tools.mkdir()
    for tool in ("dirname", "sha256sum"):
        executable = shutil.which(tool)
        assert executable
        (host_tools / tool).symlink_to(executable)

    result = subprocess.run([str(bundle / "installer/aiflow4-offline"), "--bootstrap-check"],
                            check=True, capture_output=True, text=True, env={"PATH": str(host_tools)})

    assert result.stdout.splitlines() == [str(bundle / "installer/offline.py"), "--bootstrap-check"]


def test_control_api_transport_uses_validated_tls_and_rejects_redirects():
    opener = offline._control_api_opener()
    https_handler = next(handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler))
    assert https_handler._context.verify_mode == ssl.CERT_REQUIRED
    assert https_handler._context.check_hostname is True
    redirect_handler = next(handler for handler in opener.handlers if isinstance(handler, offline._NoRedirectHandler))
    assert redirect_handler.redirect_request(None, None, 307, "Temporary Redirect", {}, "http://attacker.invalid/") is None


def test_install_rejects_http_control_url_before_host_changes_or_token_submission(tmp_path):
    bundle = _bundle(tmp_path)
    token_file = tmp_path / "join-token"
    token_file.write_text("one-use-token\n", encoding="utf-8")
    calls: list[list[str]] = []
    joins: list[tuple[str, str, dict]] = []
    installer = offline.OfflineInstaller(
        bundle, root=tmp_path / "target",
        target={"distribution": "ubuntu", "version": "24.04", "architecture": "amd64"},
        runner=lambda argv, **kwargs: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1],
        post_json=lambda base, path, payload: (joins.append((base, path, payload)), {})[1],
    )

    with pytest.raises(offline.OfflineError, match="HTTPS"):
        installer.install(control_url="http://control.internal", join_token_file=token_file)

    assert calls == []
    assert joins == []


def test_deb_packages_install_agent_and_worker_units_user_and_directories(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    offline._build_debs(packages, "v4.1.0")
    agent = packages / "aiflow4-node-agent_4.1.0_all.deb"
    worker = packages / "aiflow4-worker_4.1.0_all.deb"
    assert subprocess.run(["dpkg-deb", "-f", str(agent), "Architecture"], check=True,
                          capture_output=True, text=True).stdout.strip() == "all"
    agent_depends = subprocess.run(["dpkg-deb", "-f", str(agent), "Depends"], check=True,
                                   capture_output=True, text=True).stdout
    assert "passwd" in agent_depends
    assert subprocess.run(["dpkg-deb", "-f", str(worker), "Depends"], check=True,
                          capture_output=True, text=True).stdout.strip() == "aiflow4-node-agent"
    agent_contents = subprocess.run(["dpkg-deb", "-c", str(agent)], check=True,
                                    capture_output=True, text=True).stdout
    worker_contents = subprocess.run(["dpkg-deb", "-c", str(worker)], check=True,
                                     capture_output=True, text=True).stdout
    assert "aiflow4-node-agent.service" in agent_contents
    assert "aiflow4-worker@.service" in worker_contents
    control_dir = tmp_path / "control"
    subprocess.run(["dpkg-deb", "--control", str(agent), str(control_dir)], check=True,
                   capture_output=True, text=True)
    postinst = (control_dir / "postinst").read_text(encoding="utf-8")
    assert "useradd --system" in postinst
    assert "/srv/aiflow/accounts" in postinst
    assert "/srv/aiflow/cache/ai/tmp" in postinst


HEALTHY = {"status": "online", "heartbeat_age_seconds": 3.0, "preflight_ok": True, "missing_tools": []}


def _install(tmp_path, *, inspected_extra=None, health=None, joins=None, joined_extra=None,
             control_url="https://control.internal"):
    """Run the installer against fake system commands and a fake control API; returns (node, calls, joins, root)."""
    bundle = _bundle(tmp_path)
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        argv = [str(value) for value in argv]
        calls.append(argv)
        name = Path(argv[0]).name
        if name == "uv" and argv[1:2] == ["venv"]:
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True)
            python = venv / "bin/python"
            python.write_text("python", encoding="utf-8")
            entry_point = venv / "bin/aiflow4"
            entry_point.write_text(f"#!{python}\n# generated console entry point\n", encoding="utf-8")
            entry_point.chmod(0o755)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if name == "uv" and argv[1:2] == ["pip"]:
            assert "--offline" in argv and "--no-index" in argv
        if name == "aiflow4":
            return subprocess.CompletedProcess(argv, 0, "AI Flow v4.1.0", "")
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    joins = [] if joins is None else joins
    health_calls: list[tuple[str, str]] = []

    def post_json(base_url, path, payload):
        joins.append((base_url, path, payload))
        if path.endswith("inspect"):
            return {"name": "offline-node", "release": "v4.1.0", "stacks": ["python-3.12"],
                    **(inspected_extra or {})}
        return {"node": "offline-node", "release": "v4.1.0", "node_token": "private-node-token",
                "accounts": ["codex-01"], "worker_accounts": ["codex-01"], **(joined_extra or {})}

    token_file = tmp_path / "join-token"
    token_file.write_text("single-use-join-token\n", encoding="utf-8")
    root = tmp_path / "target"
    installer = offline.OfflineInstaller(
        bundle, root=root,
        target={"distribution": "ubuntu", "version": "24.04", "architecture": "amd64"},
        runner=runner, post_json=post_json,
        get_json=lambda _base, path, token: (health_calls.append((path, token)), health or HEALTHY)[1])
    node = installer.install(control_url=control_url, join_token_file=token_file,
                             wait_seconds=2, health_wait_seconds=2)
    return node, calls, joins, root, health_calls


def test_offline_bundle_install_succeeds_without_public_network(tmp_path):
    node, calls, joins, root, health_calls = _install(tmp_path)
    assert node == "offline-node"
    assert health_calls == [("/v1/nodes/offline-node/health", "private-node-token")]
    assert [call[1] for call in joins] == ["/v1/nodes/join-tokens/inspect", "/v1/nodes/join"]
    assert (root / "srv/aiflow/releases/v4.1.0/RELEASE").read_text().strip() == "v4.1.0"
    release = root / "srv/aiflow/releases/v4.1.0"
    entry_point = release / ".venv/bin/aiflow4"
    assert entry_point.read_text(encoding="utf-8").splitlines()[0] == f"#!{release}/.venv/bin/python"
    assert Path(entry_point.read_text(encoding="utf-8").splitlines()[0][2:]).is_file()
    assert (root / "srv/aiflow/current").is_symlink()
    assert (root / "srv/aiflow/state/node.token").read_text().strip() == "private-node-token"
    assert (root / "etc/aiflow/node.env").read_text().find('AIFLOW4_NODE_NAME="offline-node"') >= 0
    assert (root / "opt/aiflow/cli/node_modules/.bin/claude").is_symlink()
    assert (root / "usr/local/bin/codex-code-mode-host").is_symlink()
    assert (root / "usr/local/bin/python3").is_symlink()
    assert any(call[:3] == ["apt-get", "--no-download", "--yes"] for call in calls)
    assert any(call[:2] == ["docker", "load"] for call in calls)
    assert any(call[-1:] == ["aiflow4-worker@codex-01.service"] for call in calls)
    assert not any(Path(call[0]).name in {"curl", "npm", "pip", "wget"} for call in calls)


def test_offline_install_keeps_paused_account_home_but_does_not_wait_for_its_worker(tmp_path):
    node, calls, _joins, root, _health = _install(
        tmp_path, joined_extra={"accounts": ["codex-01", "claude-paused"], "worker_accounts": ["codex-01"]})

    assert node == "offline-node"
    assert (root / "srv/aiflow/accounts/claude-paused").is_dir()
    checked_units = [call[-1] for call in calls if call[:2] == ["systemctl", "is-active"]]
    assert "aiflow4-worker@codex-01.service" in checked_units
    assert "aiflow4-worker@claude-paused.service" not in checked_units


def test_offline_install_smoke_uses_internal_network_live_health_and_worker_units():
    script_path = offline.ROOT / "deploy/v4/ci/offline-install-smoke.sh"
    script = script_path.read_text(encoding="utf-8")
    target = (offline.ROOT / "deploy/v4/ci/offline-target.Dockerfile").read_text(encoding="utf-8")
    subprocess.run(["bash", "-n", str(script_path)], check=True)
    assert "offline-target.Dockerfile" in script
    assert "FROM ubuntu:24.04" in target and "systemd" in target
    assert "docker.io" not in target and "git" not in target and "uv" not in target
    assert "docker network create --driver bridge --internal" in script
    assert "docker network inspect --format '{{.Internal}}'" in script
    assert "installer/aiflow4-offline install" in script
    assert "aiflow4-node-agent.service" in script and "systemctl is-active" in script
    assert "aiflow4-worker@*.service" in script
    assert "/v1/nodes/" in script and "/health" in script
    assert "preflight_ok" in script and "heartbeat_age_seconds" in script


def test_offline_install_smoke_runs_when_real_target_inputs_are_configured():
    names = ("AIFLOW4_OFFLINE_SMOKE_BUNDLE", "AIFLOW4_OFFLINE_SMOKE_CONTROL_CONTAINER",
             "AIFLOW4_OFFLINE_SMOKE_CONTROL_URL", "AIFLOW4_OFFLINE_SMOKE_JOIN_TOKEN_FILE")
    values = [os.environ.get(name) for name in names]
    if not all(values):
        pytest.skip("set AIFLOW4_OFFLINE_SMOKE_BUNDLE, _CONTROL_CONTAINER, _CONTROL_URL and _JOIN_TOKEN_FILE "
                    "to run the Ubuntu 24.04 offline-install smoke")
    command = ["bash", str(offline.ROOT / "deploy/v4/ci/offline-install-smoke.sh"), *values]
    ca_certificate = os.environ.get("AIFLOW4_OFFLINE_SMOKE_CA_CERTIFICATE")
    if ca_certificate:
        command.append(ca_certificate)
    subprocess.run(command, check=True)


def test_install_fails_when_units_are_active_but_node_never_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "time_sleep", lambda _s: None)
    with pytest.raises(offline.OfflineError, match="healthy heartbeat"):
        _install(tmp_path, health={"status": "joined", "heartbeat_age_seconds": None, "preflight_ok": False,
                                   "missing_tools": []})


def test_install_fails_when_heartbeat_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "time_sleep", lambda _s: None)
    with pytest.raises(offline.OfflineError, match="healthy heartbeat"):
        _install(tmp_path, health={**HEALTHY, "heartbeat_age_seconds": 600})


def test_install_reports_unhealthy_test_services_when_preflight_is_red(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "time_sleep", lambda _s: None)
    with pytest.raises(offline.OfflineError, match="preflight is not green.*postgres.*image not found"):
        _install(tmp_path, health={**HEALTHY, "preflight_ok": False, "missing_services": {"postgres": "image not found"}})


def test_bundle_carries_the_test_service_images_and_install_loads_them(tmp_path):
    from aiflow.v4.core.services import PG_IMAGE, REDIS_IMAGE

    assert (offline.PG_IMAGE, offline.REDIS_IMAGE) == (PG_IMAGE, REDIS_IMAGE)
    _node, calls, _joins, _root, _health = _install(tmp_path)
    loads = [c for c in calls if c[:2] == ["docker", "load"]]
    assert any(c[-1].endswith("image/services.tar") for c in loads)


def test_install_fails_when_node_is_online_but_preflight_is_red(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "time_sleep", lambda _s: None)
    with pytest.raises(offline.OfflineError, match="preflight is not green.*composer"):
        _install(tmp_path, health={**HEALTHY, "preflight_ok": False, "missing_tools": ["composer"]})


@pytest.mark.parametrize("stacks", [["php-8.3"], ["python-3.12", "playwright"]])
def test_install_rejects_stacks_the_bundle_cannot_provide_without_consuming_the_join_token(tmp_path, stacks):
    joins: list = []
    with pytest.raises(offline.OfflineError, match="cannot provide"):
        _install(tmp_path, inspected_extra={"stacks": stacks}, joins=joins)
    assert [path for _base, path, _payload in joins] == ["/v1/nodes/join-tokens/inspect"]


def _control(records):
    return lambda deb: records[deb.name]


def test_apt_closure_accepts_complete_set_with_alternatives_and_provides(tmp_path):
    records = {
        "a.deb": {"Package": "a", "Depends": "libx (>= 1), b | zzz", "Pre-Depends": "", "Provides": ""},
        "b.deb": {"Package": "b", "Depends": "virt:any", "Pre-Depends": "", "Provides": ""},
        "c.deb": {"Package": "libx", "Depends": "", "Pre-Depends": "", "Provides": "virt (= 1)"},
    }
    offline.verify_apt_closure([tmp_path / n for n in records], [], _control(records))


def test_apt_closure_rejects_dependency_missing_from_bundle(tmp_path):
    records = {"a.deb": {"Package": "a", "Depends": "libc6 (>= 2.35), git", "Pre-Depends": "", "Provides": ""}}
    with pytest.raises(offline.OfflineError, match="libc6"):
        offline.verify_apt_closure([tmp_path / "a.deb"], [], _control(records))


def test_generated_agent_package_systemd_dependency_is_requested_for_offline_closure(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    offline._build_debs(packages, "v4.1.0")
    agent = packages / "aiflow4-node-agent_4.1.0_all.deb"

    generated_metadata = offline._deb_control(agent)
    direct_dependencies = {dependency.strip().split("(", 1)[0]
                           for dependency in generated_metadata["Depends"].split(",")}

    assert "systemd" in generated_metadata["Depends"]
    assert direct_dependencies <= set(offline.APT_PACKAGES)

