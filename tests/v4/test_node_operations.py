"""Command Center install, repair, enrollment, role and audit flows."""
from __future__ import annotations

import hashlib
import io
import shlex
import subprocess
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control import api as control_api
from aiflow.v4.control.console_auth import ROLE_PERMISSIONS, issue_session
from aiflow.v4 import cli
from aiflow.v4.node import agent, operations
from conftest import join_node as issue_and_join


SESSION_SECRET = "test-console-session-secret-at-least-32-bytes"


def test_cli_keeps_canary_commands_and_stdin_join_tokens(capsys):
    with pytest.raises(SystemExit) as join_help:
        cli.main(["node", "join", "--help"])
    assert join_help.value.code == 0
    join_options = capsys.readouterr().out
    assert "--join-token-file" in join_options and "--join-token-stdin" in join_options

    with pytest.raises(SystemExit) as canary_help:
        cli.main(["canary", "--help"])
    assert canary_help.value.code == 0
    canary_commands = capsys.readouterr().out
    assert all(action in canary_commands for action in (
        "evaluate", "record-rollback", "record-promotion", "retry-tickets"))


def sign_in(client, ctx, role="owner", subject="owner@example.test"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session(subject, role, 2_000_000_000, SESSION_SECRET))


def node_join(client, name):
    response = issue_and_join(client, name)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def test_owner_can_queue_missing_tool_install_for_online_agent_and_agent_completes_it(client, ctx):
    headers = node_join(client, "node1")
    ctx.repo().versions["tools"]["codex"] = "0.4.1"
    assert client.post("/v1/nodes/node1/heartbeat", json={"tools": {"codex": "missing"}},
                       headers=headers).status_code == 200
    sign_in(client, ctx, role="super_admin")

    queued = client.post("/v1/nodes/node1/operations", json={"action": "install", "component": "codex"})
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued" and queued.json()["components"] == ["codex", "node"]
    op = client.get("/v1/nodes/node1/operations/next", headers=headers).json()["operation"]
    assert op["id"] == queued.json()["id"] and op["payload"] == {
        "action": "install", "tools": [{"name": "codex", "version": "0.4.1", "packages": []},
                                           {"name": "node", "version": "", "packages": []}], "services": []}
    result = client.post(f"/v1/nodes/node1/operations/{op['id']}/result",
                         json={"status": "completed", "result": {"healthy": True, "tools": {"codex": "codex 0.4.1"}}},
                         headers=headers)
    assert result.status_code == 200 and result.json()["status"] == "completed"
    rows = client.get("/v1/nodes/node1/operations").json()["operations"]
    assert rows[0]["status"] == "completed" and rows[0]["actor"] == "owner@example.test"
    audit = ctx.db.one("SELECT target, action, actor, accepted, result FROM write_audit"
                       " WHERE target='node' AND action='install' ORDER BY id DESC LIMIT 1")
    assert audit == {"target": "node", "action": "install", "actor": "owner@example.test",
                     "accepted": True, "result": "queued"}


def test_untrusted_identity_headers_and_lead_cannot_install_but_lead_can_read_log(client, ctx):
    headers = node_join(client, "node1")
    client.post("/v1/nodes/node1/heartbeat", json={"tools": {"codex": "missing"}}, headers=headers)
    ctx.settings.console_session_secret = SESSION_SECRET
    no_session = client.post("/v1/nodes/node1/operations", json={"action": "install", "component": "codex"},
                             headers={"X-AIFLOW4-Role": "owner", "X-AIFLOW4-User": "attacker"})
    assert no_session.status_code == 401

    sign_in(client, ctx, "lead", "lead@example.test")
    assert client.get("/v1/console/session").json() == {
        "authenticated": True, "user": "lead@example.test", "role": "lead",
        "permissions": sorted(ROLE_PERMISSIONS["lead"])}
    refused = client.post("/v1/nodes/node1/operations", json={"action": "install", "component": "codex"})
    assert refused.status_code == 403
    assert client.get("/v1/nodes/node1/operations").status_code == 200
    assert ctx.db.one("SELECT accepted, result FROM write_audit WHERE action='install' ORDER BY id DESC LIMIT 1") == {
        "accepted": False, "result": "owner role required"}


def publish_release(tmp_path, monkeypatch, version="v4.2.1"):
    release_dir = tmp_path / version
    release_dir.mkdir()
    filename = f"aiflow4-{version}.tar.gz"
    tarball = b"signed release archive"
    digest = hashlib.sha256(tarball).hexdigest()
    (release_dir / filename).write_bytes(tarball)
    (release_dir / f"{filename}.sigstore.json").write_text('{"verificationMaterial":{}}')
    (release_dir / "SHA256SUMS").write_text(f"{digest}  {filename}\n")
    (tmp_path / "CURRENT").write_text(f"{version}\n")
    monkeypatch.setattr(control_api, "RELEASES", tmp_path)
    return tarball, digest


def test_join_command_uses_the_bound_installer_token_and_allows_signed_release_download(client, ctx, tmp_path,
                                                                                         monkeypatch):
    tarball, digest = publish_release(tmp_path, monkeypatch)
    sign_in(client, ctx)
    response = client.post("/v1/nodes/fresh-node/join-command", json={
        "name": "fresh-node", "role": "worker", "project": "TESTWP"})
    assert response.status_code == 200
    data = response.json()
    command = data["command"]
    assert "\n" not in command
    assert command.startswith("curl -fsSL https://control.example.test/v1/install.sh | sudo sh -s -- --token ")
    token = shlex.split(command)[-1]
    assert data["role"] == "worker" and data["project"] == "TESTWP" and data["expires_in_s"] == 300
    saved = ctx.db.one("SELECT role,project,used_at FROM node_join_tokens WHERE node_name='fresh-node'")
    assert saved == {"role": "worker", "project": "TESTWP", "used_at": None}

    # The join token, and only while it is unused, lets the new machine download the signed release.
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/releases/v4.2.1.tar.gz", headers=auth).content == tarball
    assert client.get("/v1/releases/v4.2.1.tar.gz.sigstore.json", headers=auth).status_code == 200
    assert client.get("/v1/releases/v4.2.1.tar.gz", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v1/nodes/join", json={"name": "fresh-node", "join_token": token}).status_code == 200
    assert client.get("/v1/releases/v4.2.1.tar.gz", headers=auth).status_code == 401


def test_join_command_requires_a_published_signed_release(client, ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(control_api, "RELEASES", tmp_path)
    sign_in(client, ctx)
    response = client.post("/v1/nodes/fresh-node/join-command", json={
        "name": "fresh-node", "role": "worker", "project": "TESTWP"})
    assert response.status_code == 409
    assert ctx.db.one("SELECT accepted, result FROM write_audit WHERE action='join_command' ORDER BY id DESC"
                      " LIMIT 1") == {"accepted": False, "result": "no signed release is published"}


@pytest.mark.parametrize("control_url", [
    "", "http://127.0.0.1:18480", "http://localhost:18480", "http://169.254.0.1", "http://0.0.0.0",
    "http://127.1", "http://2130706433", "http://192.0.2.1", "http://[::ffff:127.0.0.1]",
    "file:///tmp/control", "https://owner:secret@control.example.test", "https://control.example.test/path?q=x",
    "https://control.example.test?",
])
def test_join_command_requires_an_explicit_non_local_control_url_before_creating_token(
        client, ctx, tmp_path, monkeypatch, control_url):
    publish_release(tmp_path, monkeypatch)
    ctx.settings.join_control_url = control_url
    sign_in(client, ctx)
    response = client.post("/v1/nodes/fresh-node/join-command", json={
        "name": "fresh-node", "role": "worker", "project": "TESTWP"})
    assert response.status_code == 503
    assert ctx.db.one("SELECT count(*) AS n FROM node_join_tokens WHERE node_name='fresh-node'")["n"] == 0
    audit = ctx.db.one("SELECT accepted FROM write_audit WHERE action='join_command' ORDER BY id DESC LIMIT 1")
    assert audit == {"accepted": False}


def test_join_control_url_accepts_an_operator_routed_private_address():
    assert control_api.validated_join_control_url("http://10.44.0.10:18480/") == "http://10.44.0.10:18480"


def test_join_command_uses_a_single_use_token_and_never_accepts_ssh_password(client, ctx, tmp_path, monkeypatch):
    publish_release(tmp_path, monkeypatch)
    sign_in(client, ctx)
    response = client.post("/v1/nodes/fresh-node/join-command", json={
        "name": "fresh-node", "role": "worker", "project": "TESTWP"})
    assert response.status_code == 200
    data = response.json()
    command = data["command"]
    token = shlex.split(command)[-1]
    saved = ctx.db.one("SELECT token_hash, node_name, role, project, used_at FROM node_join_tokens"
                       " WHERE node_name='fresh-node'")
    assert saved == {"token_hash": hashlib.sha256(token.encode()).hexdigest(), "node_name": "fresh-node",
                     "role": "worker", "project": "TESTWP", "used_at": None}
    audit = ctx.db.one("SELECT payload FROM write_audit WHERE action='join_command' ORDER BY id DESC LIMIT 1")
    assert audit["payload"] == {"expires_at": data["expires_at"], "role": "worker", "project": "TESTWP",
                                "ssh_password_saved": False}
    assert "ssh_password" not in audit["payload"]
    assert client.post("/v1/nodes/fresh-node/join-command",
                       json={"name": "fresh-node", "role": "worker", "project": "TESTWP",
                             "ssh_password": "not-accepted"}).status_code == 422

    joined = client.post("/v1/nodes/join", json={"name": "fresh-node", "join_token": token})
    assert joined.status_code == 200
    assert client.post("/v1/nodes/join", json={"name": "fresh-node", "join_token": token}).status_code == 403
    assert ctx.db.one("SELECT used_at IS NOT NULL AS consumed FROM node_join_tokens WHERE node_name='fresh-node'")[
        "consumed"] is True


def test_repair_plan_uses_project_toolchains_versions_lock_and_desired_services(client, ctx):
    headers = node_join(client, "node1")
    repo = ctx.repo()
    repo.projects["TESTWP"].stack = "php-8.3"
    repo.nodes["node1"].packages = ["curl", "ca-certificates"]
    repo.nodes["node1"].services = [{"name": "worker-api", "unit": "worker-api.service",
                                     "healthcheck": "http://127.0.0.1:9000/health"}]
    repo.versions["tools"].update({"codex": "0.4.1", "php": "8.3.7", "composer": "2.8.1"})
    client.post("/v1/nodes/node1/heartbeat", json={"tools": {"git": "missing"}}, headers=headers)
    sign_in(client, ctx)

    response = client.post("/v1/nodes/node1/operations", json={"action": "repair"})
    assert response.status_code == 202
    op = client.get("/v1/nodes/node1/operations/next", headers=headers).json()["operation"]
    plan = {tool["name"]: tool["version"] for tool in op["payload"]["tools"]}
    assert set(repo.tools_for_node("node1")).issubset(plan)
    assert plan["codex"] == "0.4.1" and plan["php"] == "8.3.7" and plan["composer"] == "2.8.1"
    package_plan = next(tool for tool in op["payload"]["tools"] if tool["name"] == "packages")
    assert package_plan["packages"] == ["curl", "ca-certificates"]
    assert next(tool for tool in op["payload"]["tools"] if tool["name"] == "node")
    assert op["payload"]["services"] == repo.nodes["node1"].services


def test_repair_executor_installs_pinned_cli_and_waits_for_green_service_check(monkeypatch):
    calls = []
    versions = iter([{"node": "node v22.4.0", "codex": "missing"},
                     {"node": "node v22.4.0", "codex": "codex 0.4.1"}])
    monkeypatch.setattr(agent, "tool_versions", lambda _names: next(versions))
    monkeypatch.setattr(operations.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(operations.subprocess, "run", fake_run)
    monkeypatch.setattr(operations.httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200))
    result = operations.execute({"payload": {"action": "repair", "tools": [
        {"name": "codex", "version": "0.4.1"}, {"name": "node", "version": ""}], "services": [
        {"name": "worker-api", "unit": "worker-api.service", "healthcheck": "http://127.0.0.1/health"}]}})
    assert result["healthy"] is True
    assert result["services"] == [{"name": "worker-api", "unit": "worker-api.service", "healthy": True,
                                    "check": "green"}]
    assert calls[0] == ["npm", "install", "--global", "--no-audit", "--no-fund", "--registry",
                        "https://registry.npmjs.org", "@openai/codex@0.4.1"]
    assert calls[1:3] == [["systemctl", "enable", "--now", "worker-api.service"],
                          ["systemctl", "is-active", "--quiet", "worker-api.service"]]


def test_repair_installs_codex_when_reported_version_has_a_matching_prefix(monkeypatch):
    snapshots = iter([{"node": "node v22.4.0", "codex": "codex 0.4.10"},
                      {"node": "node v22.4.0", "codex": "codex 0.4.1"}])
    installs = []
    monkeypatch.setattr(agent, "tool_versions", lambda _names: next(snapshots))
    monkeypatch.setattr(operations.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(operations, "_install_component",
                        lambda name, version, packages: installs.append((name, version)))

    result = operations.execute({"payload": {"action": "repair", "tools": [
        {"name": "codex", "version": "0.4.1"}, {"name": "node", "version": ""}], "services": []}})

    assert result["healthy"] is True
    assert installs == [("codex", "0.4.1")]
    assert not operations._tool_version_ok("codex", "codex 0.4.10", "0.4.1")
    assert operations._tool_version_ok("codex", "codex 0.4.10", "0.4")
    assert operations._tool_version_ok("python3", "Python 3.12.3", "3.12")


def test_ai_cli_repair_installs_node_when_npm_is_missing_even_if_node_is_present(monkeypatch):
    snapshots = iter([{"node": "node v22.4.0", "codex": "missing"},
                      {"node": "node v22.4.0", "codex": "codex 0.4.1"}])
    installs = []
    monkeypatch.setattr(agent, "tool_versions", lambda _names: next(snapshots))
    monkeypatch.setattr(operations.shutil, "which", lambda _name: None)
    monkeypatch.setattr(operations, "_install_component",
                        lambda name, version, packages: installs.append((name, version)))

    result = operations.execute({"payload": {"action": "install", "tools": [
        {"name": "codex", "version": "0.4.1"}, {"name": "node", "version": ""}], "services": []}})

    assert result["healthy"] is True
    assert installs == [("node", ""), ("codex", "0.4.1")]


def test_repair_installs_missing_required_toolchains_and_codex_host(monkeypatch):
    names = ["uv", "python3", "composer", "wp", "codex-code-mode-host"]
    pins = {"uv": "0.5.0", "python3": "3.12", "composer": "2.8.1", "wp": "2.12.0",
            "codex-code-mode-host": "0.4.1"}
    snapshots = iter([dict.fromkeys(names, "missing"), {
        "uv": "uv 0.5.0", "python3": "Python 3.12.3", "composer": "Composer 2.8.1",
        "wp": "WP-CLI 2.12.0", "codex-code-mode-host": "codex-code-mode-host 0.4.1",
    }])
    installs = []
    monkeypatch.setattr(agent, "tool_versions", lambda _names: next(snapshots))
    monkeypatch.setattr(operations, "_install_component", lambda name, version, packages: installs.append(name))

    result = operations.execute({"payload": {"action": "repair", "tools": [
        {"name": name, "version": pins[name], "packages": []} for name in reversed(names)], "services": []}})

    assert result["healthy"] is True
    assert installs == ["uv", "python3", "composer", "wp", "codex-code-mode-host"]


def test_agent_prepares_stack_tools_and_returns_the_post_install_preflight(monkeypatch):
    required = ["git", "uv", "php", "composer", "wp"]
    before = dict.fromkeys(required, "missing")
    after = {"git": "git 2.43", "uv": "uv 0.5.0", "php": "PHP 8.3.12",
             "composer": "Composer 2.8.9", "wp": "WP-CLI 2.12.0"}
    snapshots = iter([before, after])
    installs = []
    monkeypatch.setattr(agent, "tool_versions", lambda _names: next(snapshots))
    monkeypatch.setattr(operations, "_install_component",
                        lambda name, version, packages: installs.append((name, version)))

    result = operations.prepare_project_toolchains({"WEB": "php-8.3"},
                                                   {"tools": {"php": "8.3", "composer": "2.8", "wp": "2.12"}})

    assert [name for name, _ in installs] == ["git", "uv", "php", "composer", "wp"]
    assert dict(installs)["php"] == "8.3"
    assert result["tools"] == after and result["gaps"] == {"WEB": []}


def test_post_install_preflight_keeps_failed_tool_as_project_gap(monkeypatch):
    required = ["git", "uv", "php", "composer", "wp"]
    snapshots = iter([dict.fromkeys(required, "missing"), {
        "git": "git 2.43", "uv": "uv 0.5.0", "php": "PHP 8.3.12", "composer": "missing",
        "wp": "WP-CLI 2.12.0"}])
    installs = []
    monkeypatch.setattr(agent, "tool_versions", lambda _names: next(snapshots))

    def install(name, version, packages):
        installs.append(name)
        if name == "composer":
            raise operations.OperationError("installer failed")

    monkeypatch.setattr(operations, "_install_component", install)
    result = operations.prepare_project_toolchains(
        {"WEB": "php-8.3"}, {"tools": {"php": "8.3", "composer": "2.8", "wp": "2.12"}})

    assert "composer" in installs
    assert result["gaps"] == {"WEB": ["composer"]}


def test_composer_uses_the_official_verified_installer(monkeypatch):
    installer = b"official composer installer"
    signature = hashlib.sha384(installer).hexdigest()
    urls = []

    def response(url, **kwargs):
        urls.append(url)
        return SimpleNamespace(text=signature, content=installer)

    commands = []
    monkeypatch.setattr(operations, "_resolve_composer_version", lambda _version: "2.8.9")
    monkeypatch.setattr(operations, "_response", response)
    monkeypatch.setattr(operations.subprocess, "run", lambda command, **kwargs:
                        commands.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr=""))

    operations._install_composer("2.8")

    assert urls == ["https://composer.github.io/installer.sig", "https://getcomposer.org/installer"]
    command = commands[0][0]
    assert command[:2] == ["php", command[1]] and command[1].endswith("composer-setup.php")
    assert "--version=2.8.9" in command and "--filename=composer" in command


def test_wp_cli_uses_the_official_release_phar(monkeypatch):
    calls = []
    monkeypatch.setattr(operations, "_resolve_wp_version", lambda _version: "2.12.3")
    monkeypatch.setattr(operations, "_github_release_asset",
                        lambda *args: calls.append(("release", args)) or b"wp phar")
    monkeypatch.setattr(operations, "_install_binary",
                        lambda *args, **kwargs: calls.append(("install", args, kwargs)))

    operations._install_wp("2.12")

    assert calls == [("release", ("wp-cli/wp-cli", "v2.12.3", "wp-cli-2.12.3.phar")),
                     ("install", ("wp", b"wp phar"), {})]


def test_repair_passes_declared_os_packages_to_server_installer_module(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "tool_versions", lambda _names: {})
    monkeypatch.setattr(operations, "_run_server_installer",
                        lambda name, version, packages=None: calls.append((name, version, packages)))
    result = operations.execute({"payload": {"action": "repair", "tools": [
        {"name": "packages", "version": "", "packages": ["curl", "ca-certificates"]}], "services": []}})
    assert result["healthy"] is True
    assert calls == [("packages", "", ["curl", "ca-certificates"])]


def test_supporting_tool_installer_routes_are_whitelisted(monkeypatch):
    routed = []
    for tool, helper in (("uv", "_install_uv"), ("python3", "_install_python"), ("node", "_install_node"),
                         ("gh", "_install_gh"), ("composer", "_install_composer"), ("wp", "_install_wp"),
                         ("codex-code-mode-host", "_install_codex_host"), ("playwright", "_install_playwright"),
                         ("playwright-browser", "_install_playwright_browser")):
        monkeypatch.setattr(operations, helper, lambda version, tool=tool: routed.append((tool, version)))
        operations._install_component(tool, "1.2.3", [])
    assert routed == [(tool, "1.2.3") for tool in
                      ("uv", "python3", "node", "gh", "composer", "wp", "codex-code-mode-host", "playwright",
                       "playwright-browser")]


def test_node_24_major_line_resolves_to_latest_matching_official_release(monkeypatch):
    monkeypatch.setattr(operations, "_linux_arch", lambda: ("x86_64", "x64"))
    monkeypatch.setattr(operations, "_response", lambda *args, **kwargs: SimpleNamespace(json=lambda: [
        {"version": "v25.1.0", "lts": False}, {"version": "v24.8.1", "lts": False},
        {"version": "v22.20.0", "lts": "Jod"}]))
    assert operations._resolve_node_version("24.x") == "24.8.1"


def test_python_stack_pin_uses_uv_to_install_the_requested_minor_version(monkeypatch):
    calls = []
    monkeypatch.setattr(operations.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(operations.subprocess, "run", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    operations._install_python("3.12")
    assert calls[0][0] == ["uv", "python", "install", "3.12", "--default", "--install-dir",
                           "/usr/local/lib/aiflow4/uv-python"]
    assert calls[0][1]["env"]["UV_PYTHON_BIN_DIR"] == "/usr/local/bin"


def test_codex_host_repair_requires_the_version_recorded_by_its_installer():
    assert not operations._tool_version_ok("codex-code-mode-host", "present /usr/local/bin/codex-code-mode-host",
                                           "0.4.1")
    assert operations._tool_version_ok("codex-code-mode-host", "codex-code-mode-host 0.4.1", "0.4.1")
    assert not operations._tool_version_ok("codex-code-mode-host", "codex-code-mode-host 0.4.0", "0.4.1")


def test_codex_host_installer_uses_the_locked_release_and_installs_beside_codex(monkeypatch, tmp_path):
    calls = []
    codex_directory = tmp_path / "bin"
    codex_directory.mkdir()
    monkeypatch.setattr(operations, "_linux_arch", lambda: ("x86_64", "x64"))
    monkeypatch.setattr(operations, "_github_release_asset", lambda *args: calls.append(("asset", args)) or b"archive")
    monkeypatch.setattr(operations, "_archive_member", lambda payload, suffix: b"host binary")
    monkeypatch.setattr(operations.shutil, "which", lambda name: str(codex_directory / "codex")
                        if name == "codex" else None)
    monkeypatch.setattr(operations, "_install_binary",
                        lambda name, payload, directory: calls.append(("binary", name, payload, directory)))
    monkeypatch.setattr(operations, "_install_file",
                        lambda name, payload, directory: calls.append(("file", name, payload, directory)))
    operations._install_codex_host("0.147.0")
    assert calls[0] == ("asset", ("openai/codex", "rust-v0.147.0",
                                   "codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz"))
    assert calls[1] == ("binary", "codex-code-mode-host", b"host binary", codex_directory)
    assert calls[2] == ("file", "codex-code-mode-host.version", b"0.147.0\n", codex_directory)


def test_github_artifact_digest_is_verified_before_use(monkeypatch):
    payload = b"released tool bytes"
    digest = hashlib.sha256(payload).hexdigest()
    api_url = "https://api.github.com/repos/owner/tool/releases/tags/v1.2.3"

    class FakeResponse:
        status_code = 200
        content = payload
        text = ""

        def __init__(self, release=None):
            self.release = release

        def raise_for_status(self):
            return None

        def json(self):
            return self.release

    def fake_get(url, **kwargs):
        if url == api_url:
            return FakeResponse({"assets": [{"name": "tool.tar.gz", "digest": f"sha256:{digest}",
                                             "browser_download_url": "https://github.com/owner/tool/releases/download/v1.2.3/tool.tar.gz"}]})
        return FakeResponse()

    monkeypatch.setattr(operations.httpx, "get", fake_get)
    assert operations._github_release_asset("owner/tool", "v1.2.3", "tool.tar.gz") == payload

    monkeypatch.setattr(operations.httpx, "get", lambda url, **kwargs: FakeResponse(
        {"assets": [{"name": "tool.tar.gz", "digest": "sha256:" + "0" * 64,
                      "browser_download_url": "https://github.com/owner/tool/releases/download/v1.2.3/tool.tar.gz"}]})
        if url == api_url else FakeResponse())
    with pytest.raises(operations.OperationError, match="checksum verification failed"):
        operations._github_release_asset("owner/tool", "v1.2.3", "tool.tar.gz")


def test_node_agent_claims_whitelisted_operation_and_posts_its_result(monkeypatch):
    operation = {"id": "op-1", "payload": {"action": "repair"}}
    calls = []

    class FakeApi:
        def call(self, method, path, body=None, **kwargs):
            calls.append((method, path, body, kwargs))
            return {"operation": operation} if method == "GET" else {"status": body["status"]}

    monkeypatch.setattr(operations, "execute", lambda op: {"healthy": op is operation})
    agent.process_node_operation(FakeApi(), "node1")
    assert calls[0][0:2] == ("GET", "/v1/nodes/node1/operations/next")
    assert calls[1] == ("POST", "/v1/nodes/node1/operations/op-1/result",
                        {"status": "completed", "result": {"healthy": True}}, {"retries": 2})


def test_node_agent_keeps_heartbeating_while_an_operation_runs(monkeypatch):
    operation = {"id": "op-slow", "payload": {"action": "repair"}}
    heartbeat_count = 0
    heartbeats = threading.Event()
    execute_started = threading.Event()
    finish_execute = threading.Event()
    result_posted = threading.Event()
    operation_claimed = False

    class FakeApi:
        def call(self, method, path, body=None, **kwargs):
            nonlocal heartbeat_count, operation_claimed
            if path.endswith("/heartbeat"):
                heartbeat_count += 1
                if heartbeat_count >= 2:
                    heartbeats.set()
                return {"desired": {"workers": [], "accounts": [], "release": ""}}
            if path.endswith("/operations/next"):
                if operation_claimed:
                    return {"operation": None}
                operation_claimed = True
                return {"operation": operation}
            if path.endswith("/result"):
                result_posted.set()
                return {"status": body["status"]}
            return {}

    api = FakeApi()
    monkeypatch.setattr(agent, "ControlClient", lambda *args, **kwargs: api)
    monkeypatch.setattr(agent, "metrics", lambda previous: ({}, previous))
    monkeypatch.setattr(agent, "tool_versions", lambda: {})
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent, "installed_release", lambda: "")

    def slow_execute(_operation):
        execute_started.set()
        finish_execute.wait(2)
        return {"healthy": True}

    monkeypatch.setattr(operations, "execute", slow_execute)
    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")
    stop = threading.Event()
    runner = threading.Thread(target=agent.run_agent,
                              args=("http://control", "node1", "token"),
                              kwargs={"every_s": 0.005, "manage_workers": False, "stop": stop})
    runner.start()
    try:
        assert execute_started.wait(1)
        assert heartbeats.wait(1)
        assert heartbeat_count >= 2
    finally:
        stop.set()
        finish_execute.set()
        runner.join(2)
    assert not runner.is_alive()
    assert result_posted.wait(1)


def test_agent_retries_stack_preflight_after_release_and_versions_lock_changes(monkeypatch):
    import time

    stop = threading.Event()
    condition = threading.Condition()
    prepared = []
    desired_states = [
        {"release": "v4.1.0", "project_stacks": {"WEB": "php-8.3"}, "versions": {"tools": {"php": "8.3"}}},
        {"release": "v4.1.0", "project_stacks": {"WEB": "php-8.3"}, "versions": {"tools": {"php": "8.3"}}},
        {"release": "v4.2.0", "project_stacks": {"WEB": "php-8.3"}, "versions": {"tools": {"php": "8.3"}}},
        {"release": "v4.2.0", "project_stacks": {"WEB": "php-8.3"}, "versions": {"tools": {"php": "8.3", "composer": "2.8"}}},
        {"release": "v4.2.0", "project_stacks": {"WEB": "php-8.3"}, "versions": {"tools": {"php": "8.3", "composer": "2.8"}}},
    ]

    class FakeApi:
        def __init__(self, *args, **kwargs):
            self.heartbeats = 0

        def call(self, method, path, body=None, **kwargs):
            if path.endswith("/heartbeat"):
                self.heartbeats += 1
                required_preparations = {2: 1, 4: 2, 5: 3}.get(self.heartbeats, 0)
                if required_preparations:
                    with condition:
                        assert condition.wait_for(lambda: len(prepared) >= required_preparations, timeout=1)
                    time.sleep(0.01)
                desired = dict(desired_states[min(self.heartbeats - 1, len(desired_states) - 1)])
                if self.heartbeats == len(desired_states):
                    stop.set()
                return {"desired": {**desired, "workers": [], "accounts": []}}
            return {"operation": None}

    def prepare(stacks, versions):
        with condition:
            prepared.append((stacks, versions))
            condition.notify_all()
        return {"tools": {}}

    monkeypatch.setattr(agent, "ControlClient", FakeApi)
    monkeypatch.setattr(agent, "metrics", lambda previous: ({}, previous))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent, "tool_versions", lambda *_names: {})
    monkeypatch.setattr(agent, "installed_release", lambda: "")
    monkeypatch.setattr(operations, "prepare_project_toolchains", prepare)
    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")

    runner = threading.Thread(target=agent.run_agent, args=("http://control", "node1", "token"),
                              kwargs={"every_s": 0, "manage_workers": False, "stop": stop})
    runner.start()
    runner.join(3)
    assert not runner.is_alive()
    with condition:
        assert len(prepared) == 3
    assert prepared[0][0] == prepared[1][0] == prepared[2][0]
    assert prepared[0][1] == prepared[1][1]
    assert prepared[2][1]["tools"]["composer"] == "2.8"


def test_agent_applies_the_preflight_probe_once_and_keeps_refreshing_tool_versions(monkeypatch):
    import time

    stop = threading.Event()
    prepared = threading.Event()
    clock = {"now": 0.0}
    reported = []
    probes = []
    desired = {"release": "v4.1.0", "project_stacks": {"WEB": "php-8.3"}, "versions": {"tools": {"php": "8.3"}},
               "workers": [], "accounts": []}

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, path, body=None, **kwargs):
            if path.endswith("/heartbeat"):
                if len(reported) == 1:
                    assert prepared.wait(1)
                    time.sleep(0.02)
                reported.append(dict(body["tools"]))
                clock["now"] += 400  # one heartbeat later; the refresh is due every 600 seconds
                if len(reported) == 8:
                    stop.set()
                return {"desired": desired}
            return {"operation": None}

    def fake_tool_versions(*_names):
        probes.append(clock["now"])
        # The first probe runs at start; PHP is repaired by hand after the post-install preflight.
        return {"php": "missing"} if len(probes) == 1 else {"php": "PHP 8.3.7"}

    def prepare(stacks, versions):
        prepared.set()
        return {"tools": {"php": "missing"}}

    monkeypatch.setattr(agent, "time", SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setattr(agent, "ControlClient", FakeApi)
    monkeypatch.setattr(agent, "metrics", lambda previous: ({}, previous))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent, "tool_versions", fake_tool_versions)
    monkeypatch.setattr(agent, "installed_release", lambda: "")
    monkeypatch.setattr(operations, "prepare_project_toolchains", prepare)
    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")

    runner = threading.Thread(target=agent.run_agent, args=("http://control", "node1", "token"),
                              kwargs={"every_s": 0, "manage_workers": False, "stop": stop})
    runner.start()
    runner.join(3)
    assert not runner.is_alive()
    assert len(reported) == 8
    assert reported[2]["php"] == "missing"  # the post-install probe is reported once
    assert len(probes) >= 2  # the 10-minute tool_versions() refresh still runs after preparation
    assert reported[-1]["php"] == "PHP 8.3.7"


def test_server_installer_argv_comes_from_the_module_whitelist():
    binary = "/opt/server-installer/v1.1.0/bin/server-installer"
    assert str(operations.SERVER_INSTALLER) == binary
    assert operations.server_installer_command("packages") == ([binary, "apply", "--modules", "packages"], {})
    assert operations.server_installer_command("git", "2.43") == ([binary, "apply", "--modules", "packages"], {})
    assert operations.server_installer_command("docker", "27.5") == ([binary, "apply", "--modules", "docker"], {})
    assert operations.server_installer_command("php", "8.3.7") == (
        [binary, "apply", "--modules", "php"], {"SI_PHP_VERSION": "8.3"})
    for unknown in ("nginx", "docker,php", "packages --profile web-lemp", "uv", ""):
        with pytest.raises(operations.OperationError, match="server-installer whitelist"):
            operations.server_installer_command(unknown)
    with pytest.raises(operations.OperationError):
        operations.server_installer_command("php", "8.3;id")


def test_packages_docker_and_php_run_the_pinned_server_installer_without_a_shell(monkeypatch, tmp_path):
    binary = tmp_path / "server-installer"
    binary.write_text("#!/bin/bash\n")
    monkeypatch.setattr(operations, "SERVER_INSTALLER", binary)
    monkeypatch.setenv("SI_PHP_MODE", "fpm")
    calls = []
    package_modules = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "aiflow4-server-installer-" in command[0]:
            module = Path(command[0]).parent.parent / "modules" / "essentials" / "packages" / "module.sh"
            package_modules.append(module.read_text())
            assert '"zip" "unzip"' in package_modules[-1]
            assert 'si_pkg_install "${SI_PACKAGES_LIST[@]}"' in package_modules[-1]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(operations.subprocess, "run", fake_run)
    operations._install_component("packages", "", ["zip", "unzip"])
    operations._install_component("docker", "27.5", [])
    operations._install_component("php", "8.3.7", [])
    installer_calls = [(c, kw) for c, kw in calls if len(c) >= 4 and c[-3] == "apply" and c[-2] == "--modules"]
    assert len(installer_calls) == 3
    assert installer_calls[0][0][-3:] == ["apply", "--modules", "packages"]
    assert installer_calls[1][0] == [str(binary), "apply", "--modules", "docker"]
    assert installer_calls[2][0] == [str(binary), "apply", "--modules", "php"]
    assert all("shell" not in kw for _, kw in installer_calls)
    assert installer_calls[2][1]["env"]["SI_PHP_VERSION"] == "8.3"
    assert all("SI_PHP_MODE" not in kw["env"] for _, kw in installer_calls)
    assert package_modules[0].count('"zip"') == 1 and package_modules[0].count('"unzip"') == 1


def test_missing_server_installer_is_provisioned_from_its_pinned_archive(monkeypatch, tmp_path):
    install_dir = tmp_path / "opt" / "server-installer" / "v1.1.0"
    binary = install_dir / "bin" / "server-installer"
    monkeypatch.setattr(operations, "SERVER_INSTALLER", binary)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        cli = b"#!/bin/bash\n"
        member = tarfile.TarInfo("server-installer-v1.1.0/bin/server-installer")
        member.mode = 0o755
        member.size = len(cli)
        archive.addfile(member, io.BytesIO(cli))
    downloads = []

    def fake_response(url, **kwargs):
        downloads.append((url, kwargs))
        return SimpleNamespace(content=payload.getvalue())

    calls = []
    monkeypatch.setattr(operations, "_response", fake_response)
    monkeypatch.setattr(operations.subprocess, "run", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr="")))

    operations._install_component("docker", "27.5", [])

    assert downloads == [("https://api.github.com/repos/jooservices/server-installer/tarball/v1.1.0",
                         {"timeout": 900})]
    assert binary.is_file() and binary.stat().st_mode & 0o111
    assert calls[0][0] == [str(binary), "apply", "--modules", "docker"]


def test_unsafe_extra_packages_fail_before_running_server_installer(monkeypatch, tmp_path):
    binary = tmp_path / "server-installer" / "v1.1.0" / "bin" / "server-installer"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/bash\n")
    monkeypatch.setattr(operations, "SERVER_INSTALLER", binary)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        module = Path(command[0]).parent.parent / "modules" / "essentials" / "packages" / "module.sh"
        source = module.read_text()
        assert '"curl"' in source
        assert 'si_pkg_install "${SI_PACKAGES_LIST[@]}"' in source
        assert "module_verify()" in source and "module_check" in source
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(operations.subprocess, "run", fake_run)
    with pytest.raises(operations.OperationError, match="safe package names"):
        operations._install_component("packages", "", ["curl;id"])
    assert calls == []


def test_operation_whitelist_rejects_arbitrary_commands():
    with pytest.raises(operations.OperationError, match="not in the install whitelist"):
        operations._install_component("bash -c whoami", "", [])
    with pytest.raises(operations.OperationError, match="not in the install whitelist"):
        operations._install_component("bash", "", [])
    with pytest.raises(operations.OperationError, match="whitelist"):
        operations.execute({"payload": {"action": "shell", "command": "id"}})
