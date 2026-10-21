"""Command Center install, repair, enrollment, role and audit flows."""
from __future__ import annotations

import hashlib
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from aiflow.v4.control import api as control_api
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.node import agent, operations


SESSION_SECRET = "test-console-session-secret-at-least-32-bytes"


def sign_in(client, ctx, role="owner", subject="owner@example.test"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session(subject, role, 2_000_000_000, SESSION_SECRET))


def node_join(client, name):
    response = client.post("/v1/nodes/join", json={"name": name, "join_token": "join-secret"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def test_owner_can_queue_missing_tool_install_for_online_agent_and_agent_completes_it(client, ctx):
    headers = node_join(client, "node1")
    ctx.repo().versions["tools"]["codex"] = "0.4.1"
    assert client.post("/v1/nodes/node1/heartbeat", json={"tools": {"codex": "missing"}},
                       headers=headers).status_code == 200
    sign_in(client, ctx)

    queued = client.post("/v1/nodes/node1/operations", json={"action": "install", "component": "codex"})
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued" and queued.json()["components"] == ["codex"]
    op = client.get("/v1/nodes/node1/operations/next", headers=headers).json()["operation"]
    assert op["id"] == queued.json()["id"] and op["payload"] == {
        "action": "install", "tools": [{"name": "codex", "version": "0.4.1", "packages": []}], "services": []}
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
        "authenticated": True, "user": "lead@example.test", "role": "lead"}
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


def bootstrap_script(command):
    prefix, suffix = "printf '%s\\n' ", " | sudo bash -s"
    assert command.startswith(prefix) and command.endswith(suffix)
    [script] = shlex.split(command[len(prefix):-len(suffix)])
    return script


def test_join_command_bootstraps_a_verified_release_before_joining(client, ctx, tmp_path, monkeypatch):
    tarball, digest = publish_release(tmp_path, monkeypatch)
    sign_in(client, ctx)
    command = client.post("/v1/nodes/fresh-node/join-command", json={"name": "fresh-node"}).json()["command"]
    assert "\n" not in command
    script = bootstrap_script(command)
    steps = script.split("; ")
    token = next(step for step in steps if step.startswith("T="))[2:]
    assert token not in command.split(" | ")[1]  # the token reaches sudo on stdin, never as an argument
    assert "V=v4.2.1" in steps and f"S={digest}" in steps
    order = [next(i for i, step in enumerate(steps) if marker in step) for marker in (
        'get "/v1/releases/$V.tar.gz"', "sha256sum -c", "cosign verify-blob",
        "deploy/v4/bin/aiflow4-node-install", "/srv/aiflow/current/.venv/bin/aiflow4 node join",
        "systemctl restart aiflow4-node-agent")]
    assert order == sorted(order)
    assert "@refs/tags/$V" not in script and "release-v4.yml@refs/tags/v4.2.1" in script
    assert " aiflow4 node join" not in script.replace("/.venv/bin/aiflow4 node join", "")
    assert subprocess.run(["bash", "-n", "-c", script]).returncode == 0

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
    response = client.post("/v1/nodes/fresh-node/join-command", json={"name": "fresh-node"})
    assert response.status_code == 409
    assert ctx.db.one("SELECT accepted, result FROM write_audit WHERE action='join_command' ORDER BY id DESC"
                      " LIMIT 1") == {"accepted": False, "result": "no signed release is published"}


def test_join_command_uses_a_single_use_token_and_never_accepts_ssh_password(client, ctx, tmp_path, monkeypatch):
    publish_release(tmp_path, monkeypatch)
    sign_in(client, ctx)
    response = client.post("/v1/nodes/fresh-node/join-command", json={"name": "fresh-node"})
    assert response.status_code == 200
    command = response.json()["command"]
    assert "--join-token-stdin" in command
    token = next(step for step in bootstrap_script(command).split("; ") if step.startswith("T="))[2:]
    saved = ctx.db.one("SELECT token_hash, node, consumed_at FROM node_join_tokens WHERE node='fresh-node'")
    assert saved["token_hash"] == hashlib.sha256(token.encode()).hexdigest() and saved["consumed_at"] is None
    audit = ctx.db.one("SELECT payload FROM write_audit WHERE action='join_command' ORDER BY id DESC LIMIT 1")
    assert audit["payload"] == {"expires_at": response.json()["expires_at"], "ssh_password_saved": False}
    assert "ssh_password" not in audit["payload"]
    assert client.post("/v1/nodes/fresh-node/join-command",
                       json={"name": "fresh-node", "ssh_password": "not-accepted"}).status_code == 422

    joined = client.post("/v1/nodes/join", json={"name": "fresh-node", "join_token": token})
    assert joined.status_code == 200
    assert client.post("/v1/nodes/join", json={"name": "fresh-node", "join_token": token}).status_code == 403
    assert ctx.db.one("SELECT consumed_at IS NOT NULL AS consumed FROM node_join_tokens WHERE node='fresh-node'")[
        "consumed"] is True


def test_repair_plan_uses_project_toolchains_versions_lock_and_desired_services(client, ctx):
    headers = node_join(client, "node1")
    repo = ctx.repo()
    repo.projects["TESTWP"].stack = "php-8.3"
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
    assert op["payload"]["services"] == repo.nodes["node1"].services


def test_repair_executor_installs_pinned_cli_and_waits_for_green_service_check(monkeypatch):
    calls = []
    versions = iter([{"codex": "missing"}, {"codex": "codex 0.4.1"}])
    monkeypatch.setattr(agent, "tool_versions", lambda _names: next(versions))

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(operations.subprocess, "run", fake_run)
    monkeypatch.setattr(operations.httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200))
    result = operations.execute({"payload": {"action": "repair", "tools": [
        {"name": "codex", "version": "0.4.1"}], "services": [
        {"name": "worker-api", "unit": "worker-api.service", "healthcheck": "http://127.0.0.1/health"}]}})
    assert result["healthy"] is True
    assert result["services"] == [{"name": "worker-api", "unit": "worker-api.service", "healthy": True,
                                    "check": "green"}]
    assert calls[0] == ["npm", "install", "--global", "--no-audit", "--no-fund", "@openai/codex@0.4.1"]
    assert calls[1:3] == [["systemctl", "enable", "--now", "worker-api.service"],
                          ["systemctl", "is-active", "--quiet", "worker-api.service"]]


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

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        installed = command[0] == "dpkg-query"
        return SimpleNamespace(returncode=0, stdout="install ok installed" if installed else "", stderr="")

    monkeypatch.setattr(operations.subprocess, "run", fake_run)
    operations._install_component("packages", "", ["zip", "unzip"])
    operations._install_component("docker", "27.5", [])
    operations._install_component("php", "8.3.7", [])
    installer_calls = [(c, kw) for c, kw in calls if c[0] == str(binary)]
    assert [c for c, _ in installer_calls] == [[str(binary), "apply", "--modules", m]
                                               for m in ("packages", "docker", "php")]
    assert all("shell" not in kw for _, kw in installer_calls)
    assert installer_calls[2][1]["env"]["SI_PHP_VERSION"] == "8.3"
    assert all("SI_PHP_MODE" not in kw["env"] for _, kw in installer_calls)
    assert [c for c, _ in calls if c[0] == "dpkg-query"] == [
        ["dpkg-query", "-W", "-f=${Status}", "zip"], ["dpkg-query", "-W", "-f=${Status}", "unzip"]]


def test_missing_server_installer_or_extra_packages_fail_the_operation(monkeypatch, tmp_path):
    monkeypatch.setattr(operations, "SERVER_INSTALLER", tmp_path / "absent")
    with pytest.raises(operations.OperationError, match="server-installer v1.1.0 is not installed"):
        operations._install_component("docker", "", [])
    binary = tmp_path / "server-installer"
    binary.write_text("#!/bin/bash\n")
    monkeypatch.setattr(operations, "SERVER_INSTALLER", binary)
    monkeypatch.setattr(operations.subprocess, "run", lambda command, **kw: SimpleNamespace(
        returncode=1 if command[0] == "dpkg-query" else 0, stdout="", stderr=""))
    with pytest.raises(operations.OperationError, match="not provided by the server-installer packages module: curl"):
        operations._install_component("packages", "", ["curl"])


def test_operation_whitelist_rejects_arbitrary_commands():
    with pytest.raises(operations.OperationError, match="not in the install whitelist"):
        operations._install_component("bash -c whoami", "", [])
    with pytest.raises(operations.OperationError, match="not in the install whitelist"):
        operations._install_component("uv", "0.5.0", [])
    with pytest.raises(operations.OperationError, match="whitelist"):
        operations.execute({"payload": {"action": "shell", "command": "id"}})
