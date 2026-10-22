"""Offline bundle inventory, Debian package contents and network-free node installation."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
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
    (bundle / "wheels/ai_flow-4.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (bundle / "python/runtime/cpython-3.12.3/bin/python3.12").write_text("python", encoding="utf-8")
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
    (bundle / "installer/aiflow4-offline").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
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


def test_deb_packages_install_agent_and_worker_units_user_and_directories(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    offline._build_debs(packages, "v4.1.0")
    agent = packages / "aiflow4-node-agent_4.1.0_all.deb"
    worker = packages / "aiflow4-worker_4.1.0_all.deb"
    assert subprocess.run(["dpkg-deb", "-f", str(agent), "Architecture"], check=True,
                          capture_output=True, text=True).stdout.strip() == "all"
    assert "passwd" in subprocess.run(["dpkg-deb", "-f", str(agent), "Depends"], check=True,
                                      capture_output=True, text=True).stdout
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


def test_offline_bundle_install_succeeds_without_public_network(tmp_path):
    bundle = _bundle(tmp_path)
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        argv = [str(value) for value in argv]
        calls.append(argv)
        name = Path(argv[0]).name
        if name == "uv" and argv[1:2] == ["venv"]:
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_text("python", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if name == "uv" and argv[1:2] == ["pip"]:
            assert "--offline" in argv and "--no-index" in argv
        if name == "aiflow4":
            return subprocess.CompletedProcess(argv, 0, "AI Flow v4.1.0", "")
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    joins: list[tuple[str, str, dict]] = []

    def post_json(base_url, path, payload):
        joins.append((base_url, path, payload))
        if path.endswith("inspect"):
            return {"name": "offline-node", "release": "v4.1.0"}
        return {"node": "offline-node", "release": "v4.1.0", "node_token": "private-node-token",
                "accounts": ["codex-01"]}

    token_file = tmp_path / "join-token"
    token_file.write_text("single-use-join-token\n", encoding="utf-8")
    root = tmp_path / "target"
    installer = offline.OfflineInstaller(
        bundle, root=root,
        target={"distribution": "ubuntu", "version": "24.04", "architecture": "amd64"},
        runner=runner, post_json=post_json)
    assert installer.install(control_url="http://control.internal", join_token_file=token_file, wait_seconds=2) == "offline-node"
    assert [call[1] for call in joins] == ["/v1/nodes/join-tokens/inspect", "/v1/nodes/join"]
    assert (root / "srv/aiflow/releases/v4.1.0/RELEASE").read_text().strip() == "v4.1.0"
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

