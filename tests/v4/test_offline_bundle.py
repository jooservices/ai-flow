import hashlib
import importlib.util
import json
import platform
import subprocess
import tarfile
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path("deploy/v4/offline/offline_bundle.py").resolve()
SPEC = importlib.util.spec_from_file_location("aiflow4_offline_bundle", SCRIPT)
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


def _lock(tmp_path, *, grok=True):
    content = """tools:
  claude_code: 2.1.4
  codex: 0.18.0
  opencode: 1.2.3
  node: v22.15.0
  uv: 0.7.4
  python3: 3.12
"""
    if grok:
        content += "  grok:\n    version: 0.9.1\n"
    path = tmp_path / "versions.lock"
    path.write_text(content)
    return path


def test_versions_lock_resolves_tool_aliases_and_keeps_grok_optional(tmp_path):
    lock = _lock(tmp_path)

    required = offline.read_versions_lock(lock)
    assert required == {
        "claude_code": "2.1.4", "codex": "0.18.0", "opencode": "1.2.3", "node": "22.15.0",
        "uv": "0.7.4", "python3": "3.12",
    }
    assert offline.read_versions_lock(lock, include_grok=True)["grok"] == "0.9.1"

    missing = _lock(tmp_path, grok=False)
    with pytest.raises(offline.BundleError, match="tools.grok"):
        offline.read_versions_lock(missing, include_grok=True)


def test_builder_checks_image_tag_and_cpu_architecture_in_docker_save_archive(tmp_path):
    image_archive = tmp_path / "image.tar"
    config = json.dumps({"architecture": "amd64", "os": "linux"}).encode()
    manifest = json.dumps([{"Config": "image-config.json", "RepoTags": ["example/control:v4.2.0"],
                            "Layers": []}]).encode()
    with tarfile.open(image_archive, "w") as archive:
        for name, data in (("manifest.json", manifest), ("image-config.json", config)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    offline._verify_docker_archive(image_archive, "example/control:v4.2.0", "x86_64")
    with pytest.raises(offline.BundleError, match="expected arm64"):
        offline._verify_docker_archive(image_archive, "example/control:v4.2.0", "aarch64")
    with pytest.raises(offline.BundleError, match="does not contain"):
        offline._verify_docker_archive(image_archive, "example/control:latest", "x86_64")


def test_builder_assembles_release_image_locked_clis_wheels_debs_and_all_checksums(tmp_path, monkeypatch):
    version = "v4.2.0"
    release = tmp_path / f"aiflow4-{version}.tar.gz"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "pyproject.toml").write_text("[project]\nname='ai-flow'\n")
    (source_dir / "uv.lock").write_text("version = 1\n")
    with tarfile.open(release, "w:gz") as archive:
        archive.add(source_dir / "pyproject.toml", arcname="pyproject.toml")
        archive.add(source_dir / "uv.lock", arcname="uv.lock")
    image_ref = f"example/control:{version}"
    image = tmp_path / "image.tar"
    architecture = "amd64" if platform.machine() == "x86_64" else "arm64"
    config = json.dumps({"architecture": architecture}).encode()
    docker_manifest = json.dumps([{"Config": "image-config.json", "RepoTags": [image_ref]}]).encode()
    with tarfile.open(image, "w") as archive:
        for name, data in (("manifest.json", docker_manifest), ("image-config.json", config)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    lock = _lock(tmp_path)

    def stage_node(_version, _target, payload, _work):
        (payload / "node/bin").mkdir(parents=True)
        (payload / "node/bin/node").write_text("node")

    def stage_clis(versions, payload, _work, include_grok):
        assert include_grok is False
        cli = payload / "cli"
        for command, package, version_key in (
                ("claude", "@anthropic-ai/claude-code", "claude_code"),
                ("codex", "@openai/codex", "codex"), ("opencode", "opencode-ai", "opencode")):
            (cli / "bin").mkdir(parents=True, exist_ok=True)
            (cli / "bin" / command).write_text(command)
            metadata = cli / "lib/node_modules" / package / "package.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(json.dumps({"name": package, "version": versions[version_key]}))

    def stage_wheels(_release_root, _versions, payload, _work, _target):
        (payload / "bin").mkdir(parents=True)
        (payload / "bin/uv").write_text("uv")
        (payload / "python/bin").mkdir(parents=True)
        (payload / "python/bin/python3.12").write_text("python")
        (payload / "wheelhouse").mkdir()
        (payload / "wheelhouse/ai_flow-4.2.0-py3-none-any.whl").write_bytes(b"application wheel")
        (payload / "wheelhouse/fastapi-0.1-py3-none-any.whl").write_bytes(b"dependency wheel")
        (payload / "requirements.txt").write_text("fastapi==0.1\n")

    def stage_debs(destination, _version):
        destination.mkdir(parents=True, exist_ok=True)
        debs = []
        for name in ("aiflow4-node-agent", "aiflow4-worker"):
            deb = destination / f"{name}_4.2.0_all.deb"
            deb.write_bytes(name.encode())
            debs.append(deb)
        return debs

    monkeypatch.setattr(offline, "_node_archive", stage_node)
    monkeypatch.setattr(offline, "_install_cli_packages", stage_clis)
    monkeypatch.setattr(offline, "_build_wheelhouse", stage_wheels)
    monkeypatch.setattr(offline, "build_deb_packages", stage_debs)
    output = offline.build_bundle(version=version, release=release, image=image, image_ref=image_ref,
                                  versions_lock=lock, output=tmp_path / "offline.tar.gz")
    extracted = offline._extract_bundle(output, tmp_path / "unpacked")
    manifest = offline.verify_bundle(extracted)

    assert manifest["versions"]["claude_code"] == "2.1.4"
    assert manifest["versions"]["codex"] == "0.18.0"
    assert manifest["versions"]["opencode"] == "1.2.3"
    assert manifest["grok_included"] is False
    assert (extracted / "assets/aiflow4-v4.2.0.tar.gz").is_file()
    assert (extracted / "assets/control-image.tar").is_file()
    assert (extracted / "payload/cli/bin/claude").is_file()
    assert (extracted / "payload/cli/bin/codex").is_file()
    assert (extracted / "payload/cli/bin/opencode").is_file()
    assert (extracted / "payload/wheelhouse/ai_flow-4.2.0-py3-none-any.whl").is_file()
    assert len(manifest["files"]["packages"]) == 2
    assert offline._sha256(output) in Path(str(output) + ".sha256").read_text()


def test_offline_cli_stager_uses_exact_locked_npm_versions_and_matching_codex_host(tmp_path, monkeypatch):
    versions = {"claude_code": "2.1.4", "codex": "0.18.0", "opencode": "1.2.3"}
    payload = tmp_path / "payload"
    npm = payload / "node/bin/npm"
    npm.parent.mkdir(parents=True)
    npm.write_text("#!/bin/sh\nexit 0\n")
    work = tmp_path / "work"
    work.mkdir()
    commands = []
    original_run = offline._run

    def run(args, **kwargs):
        commands.append(args)
        if args[0] == str(npm):
            prefix = Path(args[args.index("--prefix") + 1])
            packages = {
                "@anthropic-ai/claude-code": versions["claude_code"],
                "@openai/codex": versions["codex"], "opencode-ai": versions["opencode"],
            }
            for package, version in packages.items():
                path = prefix / "lib/node_modules" / package / "package.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"name": package, "version": version}))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return original_run(args, **kwargs)

    def download(_url, destination):
        host_dir = tmp_path / "host-src"
        host_dir.mkdir(exist_ok=True)
        host = host_dir / "codex-code-mode-host"
        host.write_bytes(b"locked codex host")
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(host, arcname="codex-code-mode-host")

    monkeypatch.setattr(offline, "_run", run)
    monkeypatch.setattr(offline, "_download", download)
    offline._install_cli_packages(versions, payload, work, include_grok=False)

    install = next(command for command in commands if command[0] == str(npm))
    assert install[-3:] == ["@anthropic-ai/claude-code@2.1.4", "@openai/codex@0.18.0", "opencode-ai@1.2.3"]
    host_dir = payload / "cli/lib/node_modules/@openai/codex/bin"
    assert (host_dir / "codex-code-mode-host").read_bytes() == b"locked codex host"
    assert (host_dir / "codex-code-mode-host.version").read_text() == "0.18.0\n"


def test_wheelhouse_stages_locked_python_uv_and_native_312_wheels(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    payload = tmp_path / "payload"
    payload.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    uv = tmp_path / "uv"
    uv.write_text("uv test fixture")
    uv.chmod(0o755)
    monkeypatch.setattr(offline.shutil, "which",
                        lambda command: str(uv) if command == "uv" else offline.sys.executable if command == "python3" else None)
    versions = {"uv": "0.7.4", "python3": "3.12.8"}
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if args[0] == str(payload / "python/bin/python3.12"):
            return SimpleNamespace(returncode=0, stdout="Python 3.12.8\n", stderr="")
        if args[-1:] == ["--version"]:
            return SimpleNamespace(returncode=0, stdout="uv 0.7.4 (test)\n", stderr="")
        if args[1:3] == ["python", "install"]:
            python = Path(args[args.index("--install-dir") + 1]) / "cpython/bin/python3.12"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\necho Python 3.12.8\n")
            python.chmod(0o755)
        elif args[1] == "export":
            Path(args[args.index("--output-file") + 1]).write_text("fastapi==0.1 --hash=sha256:" + "a" * 64 + "\n")
        elif args[1] == "build":
            wheel_dir = Path(args[args.index("--out-dir") + 1])
            (wheel_dir / "ai_flow-4.2.0-py3-none-any.whl").write_bytes(b"application wheel")
        elif args[0] == offline.sys.executable and "download" in args:
            destination = Path(args[args.index("--dest") + 1])
            (destination / "fastapi-0.1-py3-none-any.whl").write_bytes(b"dependency wheel")
        return SimpleNamespace(returncode=0, stdout="Python 3.12.8\n", stderr="")

    monkeypatch.setattr(offline, "_run", run)
    offline._build_wheelhouse(release, versions, payload, work, offline.ARCHES[platform.machine()])

    assert (payload / "python/bin/python3.12").is_symlink()
    assert (payload / "wheelhouse/ai_flow-4.2.0-py3-none-any.whl").is_file()
    pip = next(command for command in commands if command[0] == offline.sys.executable and "download" in command)
    assert pip[pip.index("--python-version") + 1] == "312"
    assert pip[pip.index("--only-binary=:all:")] == "--only-binary=:all:"
    assert pip[pip.index("--platform") + 1].startswith("manylinux_2_28_")


def test_agent_and_worker_debs_include_systemd_user_directories_and_supported_architecture(tmp_path):
    package_dir = tmp_path / "packages"
    debs = offline.build_deb_packages(package_dir, "v4.2.0")

    assert [path.name for path in debs] == ["aiflow4-node-agent_4.2.0_all.deb", "aiflow4-worker_4.2.0_all.deb"]
    for deb in debs:
        fields = subprocess.run(["dpkg-deb", "--field", str(deb)], check=True, capture_output=True, text=True).stdout
        assert "Architecture: all" in fields
        assert "Version: 4.2.0" in fields
        assert "Depends: systemd, passwd" in fields
        contents = subprocess.run(["dpkg-deb", "--contents", str(deb)], check=True,
                                  capture_output=True, text=True).stdout
        assert "lib/systemd/system/aiflow4-" in contents
        control_dir = tmp_path / "package-control" / deb.stem
        control_dir.mkdir(parents=True)
        subprocess.run(["dpkg-deb", "--control", str(deb), str(control_dir)], check=True, capture_output=True)
        postinst = (control_dir / "postinst").read_text()
        assert "useradd --system" in postinst and "aiflow-worker" in postinst
        assert all(path in postinst for path in ("/etc/aiflow", "/srv/aiflow/accounts", "/srv/aiflow/state",
                                                  "/srv/aiflow/cache/ai/tmp", "/srv/aiflow/work"))
        extracted = tmp_path / "unpacked" / deb.stem
        extracted.parent.mkdir(exist_ok=True)
        subprocess.run(["dpkg-deb", "--extract", str(deb), str(extracted)], check=True, capture_output=True)
        unit = next((extracted / "lib/systemd/system").glob("aiflow4-*.service"))
        assert "/opt/aiflow/offline/current/node/bin" in unit.read_text()

    assert set(offline.ARCHES[machine][0] for machine in ("x86_64", "aarch64")) == {"amd64", "arm64"}


def _fixture_bundle(tmp_path):
    root = tmp_path / "bundle"
    for relative in ("assets", "packages", "payload/bin", "payload/node/bin", "payload/python/bin",
                     "payload/cli/bin", "payload/wheelhouse"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "assets/aiflow4-v4.2.0.tar.gz").write_bytes(b"release archive")
    (root / "assets/control-image.tar").write_bytes(b"docker image")
    (root / "assets/versions.lock").write_text("tools: {}\n")
    (root / "packages/aiflow4-node-agent_4.2.0_all.deb").write_bytes(b"node package")
    (root / "packages/aiflow4-worker_4.2.0_all.deb").write_bytes(b"worker package")
    (root / "payload/bin/uv").write_bytes(b"uv binary")
    (root / "payload/node/bin/node").write_bytes(b"node binary")
    (root / "payload/python/bin/python3.12").write_text("python runtime")
    (root / "payload/cli/bin/codex").write_text("codex CLI")
    (root / "payload/wheelhouse/ai_flow-4.2.0-py3-none-any.whl").write_bytes(b"app wheel")
    (root / "payload/wheelhouse/fastapi-0.1-py3-none-any.whl").write_bytes(b"dependency wheel")
    (root / "payload/requirements.txt").write_text("fastapi==0.1\n")
    release_tar = root / "assets/aiflow4-v4.2.0.tar.gz"
    with tarfile.open(release_tar, "w:gz") as archive:
        contents = tmp_path / "pyproject.toml"
        contents.write_text("[project]\nname='ai-flow'\n")
        archive.add(contents, arcname="pyproject.toml")
    manifest = {
        "format": 1, "release": "v4.2.0", "architecture": "arm64", "image": "example/control:v4.2.0",
        "files": {"release": "assets/aiflow4-v4.2.0.tar.gz", "image": "assets/control-image.tar",
                  "versions_lock": "assets/versions.lock", "requirements": "payload/requirements.txt",
                  "wheelhouse": "payload/wheelhouse", "packages": [
                      "packages/aiflow4-node-agent_4.2.0_all.deb", "packages/aiflow4-worker_4.2.0_all.deb"]},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "offline_bundle.py").write_text("installer")
    offline._write_checksums(root)
    return root


def test_bundle_manifest_checks_release_image_wheels_and_debs_and_rejects_tampering(tmp_path):
    bundle = _fixture_bundle(tmp_path)
    manifest = offline.verify_bundle(bundle)
    sums = (bundle / "SHA256SUMS").read_text()

    assert manifest["files"]["image"] == "assets/control-image.tar"
    for name in ("aiflow4-v4.2.0.tar.gz", "control-image.tar", "ai_flow-4.2.0-py3-none-any.whl",
                 "fastapi-0.1-py3-none-any.whl", "aiflow4-node-agent_4.2.0_all.deb", "aiflow4-worker_4.2.0_all.deb"):
        assert name in sums
    (bundle / "assets/control-image.tar").write_bytes(b"tampered image")
    with pytest.raises(offline.BundleError, match="checksum verification failed"):
        offline.verify_bundle(bundle)


def test_offline_install_uses_bundle_and_checks_healthy_node_and_worker_without_internet(tmp_path):
    bundle = _fixture_bundle(tmp_path)
    root = tmp_path / "root"
    (root / "etc/aiflow").mkdir(parents=True)
    (root / "srv/aiflow/state").mkdir(parents=True)
    (root / "srv/aiflow/state/node.token").write_text("offline-test-node-token-123456\n")
    (root / "etc/aiflow/node.env").write_text(
        'AIFLOW4_CONTROL_URL="http://control.internal"\n'
        'AIFLOW4_NODE_NAME="offline-node"\n'
        'AIFLOW4_SLOTS="2"\n'
        'AIFLOW4_NODE_TOKEN_FILE="/srv/aiflow/state/node.token"\n')
    state = {
        "nodes": [{"name": "offline-node", "status": "online", "release": "v4.2.0",
                   "missing_tools": [], "accounts_assigned": 1}],
        "accounts": [{"id": "codex-01", "node": "offline-node", "status": "signed_out"}],
    }
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "uv" or args[0].endswith("/bin/uv"):
            if "venv" in args:
                venv = Path(args[-1])
                (venv / "bin").mkdir(parents=True, exist_ok=True)
                (venv / "bin/python").write_text("offline python")
            elif "pip" in args and "install" in args:
                assert "--offline" in args and "--no-index" in args
                assert kwargs["env"]["UV_OFFLINE"] == "1"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "curl":
            assert args[args.index("--config") + 1] == "-"
            assert kwargs["input"].startswith('header = "Authorization: Bearer ')
            target = Path(args[args.index("--output") + 1])
            target.write_text(json.dumps(state))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["systemctl", "is-active", "--quiet"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "systemctl" and "list-units" in args:
            return SimpleNamespace(returncode=0, stdout="aiflow4-worker@codex-01.service active running\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = offline.install_bundle(bundle, root=root, runner=run, wait_seconds=0)

    assert result["healthy"] is True and result["node"]["missing_tools"] == []
    assert (root / "srv/aiflow/releases/v4.2.0/RELEASE").read_text() == "v4.2.0\n"
    assert (root / "srv/aiflow/current").resolve() == (root / "srv/aiflow/releases/v4.2.0").resolve()
    assert (root / "srv/aiflow/accounts/codex-01").is_dir()
    assert any(args[0] == "docker" and args[1] == "load" for args, _ in calls)
    assert any(args[0] == "dpkg" and "-i" in args for args, _ in calls)
    assert any(args == ["systemctl", "enable", "aiflow4-node-agent.service"] for args, _ in calls)
    assert any(args == ["systemctl", "restart", "aiflow4-node-agent.service"] for args, _ in calls)
    assert any(args[0] == "systemctl" and "list-units" in args for args, _ in calls)
    assert all(args[0] not in {"apt", "apt-get", "npm", "git", "pip"} for args, _ in calls)
    assert all(not any("login" in str(argument) for argument in args) for args, _ in calls)


def test_offline_install_fails_before_host_changes_when_checksum_is_invalid(tmp_path):
    bundle = _fixture_bundle(tmp_path)
    (bundle / "payload/requirements.txt").write_text("changed\n")
    calls = []

    with pytest.raises(offline.BundleError, match="checksum verification failed"):
        offline.install_bundle(bundle, root=tmp_path / "root", runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []
