import threading
import os
import pwd
import stat
from types import SimpleNamespace

from aiflow.v4.node import agent


def test_reconcile_can_prepare_a_new_console_account_home(monkeypatch, tmp_path):
    monkeypatch.setattr(pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    assert agent.ensure_account_home("codex-node-01.2", str(tmp_path)) is True
    home = tmp_path / "codex-node-01"
    assert home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700
    assert agent.ensure_account_home("../../outside", str(tmp_path)) is False


def test_node_tool_versions_include_codex_code_mode_host(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "sh" and "codex-code-mode-host" in cmd[-1]:  # looked up next to the codex binary
            return SimpleNamespace(returncode=0, stdout="present /opt/aiflow/tools/codex/codex-code-mode-host\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} 1.0\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["codex-code-mode-host"].startswith("present ")
    assert versions["wp"] == "wp 1.0" and ["wp", "--version", "--allow-root"] in calls


def test_node_tool_versions_report_codex_code_mode_host_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "sh" and "codex-code-mode-host" in cmd[-1]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert agent.tool_versions()["codex-code-mode-host"] == "missing"


def test_node_tool_versions_report_nonzero_exit_with_output_as_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "php":
            return SimpleNamespace(returncode=1, stdout="", stderr="PHP startup error\n")
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["php"] == "missing"
    assert versions["composer"] == "composer version"


def test_node_tool_versions_report_success_without_output_as_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "composer":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["composer"] == "missing"
    assert versions["php"] == "php version"


def test_scratch_cleanup_that_restores_disk_minimum_is_reported_as_fixed(monkeypatch):
    gib = 1024 ** 3
    free_space = iter((4 * gib, 6 * gib))
    monkeypatch.setattr(agent, "_disk_free_bytes", lambda: next(free_space))
    monkeypatch.setattr(agent, "clean_scratch", lambda: 1)
    auto_fixed_conditions = set()

    assert agent._run_disk_cleanup(agent.clean_scratch, auto_fixed_conditions) == 1
    assert auto_fixed_conditions == {"disk-space"}
    disk = next(condition for condition in agent._system_machine_conditions({"disk_free": 6 * gib},
                                                                            auto_fixed_conditions)
                if condition["id"] == "disk-space")
    assert disk["result"] == "fixed_automatically"


def test_node_tool_versions_report_stack_tools(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "wp":
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    versions = agent.tool_versions()
    assert versions["php"] == "php version"
    assert versions["composer"] == "composer version"
    assert versions["python3"] == "python3 version"
    assert versions["wp"] == "missing"


def test_node_tool_versions_probe_each_playwright_browser_and_name_missing_engine(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd == ["playwright", "--version"]:
            return SimpleNamespace(returncode=0, stdout="Version 1.56.2\n", stderr="")
        if cmd[:2] == ["node", "-e"]:
            assert kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"].endswith("playwright-browser")
            if cmd[-1] == "firefox":
                return SimpleNamespace(returncode=1, stdout="", stderr="Executable doesn't exist at firefox")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    monkeypatch.setattr(agent.shutil, "which", lambda name: {
        "playwright": "/opt/node_modules/playwright/cli.js", "node": "/usr/bin/node"}.get(name))
    result = agent.tool_versions(["playwright", "chromium", "firefox", "webkit"])
    assert result == {"playwright": "Version 1.56.2", "chromium": "Chromium Playwright 1.56.2",
                      "firefox": "missing", "webkit": "WebKit Playwright 1.56.2"}
    # A missing browser requirement names the actual browser, not an executable-like tool label.
    from aiflow.v4.core.toolchains import missing_tools, toolchain_gaps
    required = ["playwright", "chromium", "firefox", "webkit"]
    assert missing_tools(result, required) == ["firefox"]
    assert toolchain_gaps(result, required, {"tools": {"playwright": "1.56"}}, ["playwright"],
                          require_pins=True) == ["firefox"]


def test_collect_heartbeat_gathers_metrics_tools_workers_and_settings(monkeypatch):
    import time

    workers = [{"account": "codex-01", "state": "working", "alive": True,
                "probe": {"credential_expires_at": "2026-10-25T10:00:00+00:00"}},
               {"account": "claude-01", "state": "idle", "alive": True}]
    node_metrics = {"cpu_pct": 23.5, "mem_used": 200, "disk_free": 300, "load1": 0.5}
    state = agent._AgentLoopState(tools={"codex": "Codex 1.0"}, tools_at=time.time(),
                                 project_environment={"DEMO": ["API_KEY", "EMPTY"]},
                                 service_state={"redis": {"healthy": True}})

    class Relay:
        def health(self):
            return {"enabled": True, "healthy": True}

    monkeypatch.setattr(agent, "metrics", lambda previous: (node_metrics, (100, 20)))
    monkeypatch.setattr(agent, "worker_states", lambda: workers)
    monkeypatch.setattr(agent, "read_env_file", lambda _path: {"API_KEY": "set", "EMPTY": ""})
    monkeypatch.setattr(agent, "runtime_settings_inventory", lambda: {"DEMO": [{"name": "API_KEY"}]})
    monkeypatch.setattr(agent, "installed_release", lambda: "v4.2.0")
    monkeypatch.setattr(agent.socket, "gethostname", lambda: "node-test")
    monkeypatch.setattr(agent, "tool_versions", lambda: (_ for _ in ()).throw(AssertionError("refresh too early")))

    heartbeat = agent.collect_heartbeat(state, "node-test", 4, Relay(), "public-key")

    readiness = {key: heartbeat.pop(key) for key in ("system_conditions", "credential_checks",
                                                     "auto_fixed_tools", "auto_fixed_conditions")}
    assert readiness["auto_fixed_tools"] == [] and readiness["auto_fixed_conditions"] == []
    assert set(readiness["credential_checks"]) == {"codex-01", "claude-01"}
    assert heartbeat == {
        "slots_total": 4, "slots_used": 1, "release_version": "v4.2.0", "release_protocol": 1,
        "tools": {"codex": "Codex 1.0"}, "services": {"redis": {"healthy": True}},
        "metrics": {**node_metrics, "hostname": "node-test",
                    "policy_relay": {"enabled": True, "healthy": True}},
        "workers": workers,
        "credential_expiries": {"codex-01": "2026-10-25T10:00:00+00:00"},
        "runtime_settings": {"DEMO": [{"name": "API_KEY"}]},
        "runtime_settings_public_key": "public-key", "environment_items": {"DEMO": ["API_KEY"]},
    }
    assert state.previous_metrics == (100, 20)


def test_send_heartbeat_posts_collected_body_and_returns_desired():
    heartbeat = {"slots_total": 2, "workers": []}
    desired = {"workers": ["codex-01"]}

    class FakeApi:
        def call(self, method, path, body, **kwargs):
            assert (method, path, body, kwargs) == (
                "POST", "/v1/nodes/node-test/heartbeat", heartbeat, {"retries": 2})
            return {"desired": desired}

    assert agent.send_heartbeat(FakeApi(), "node-test", heartbeat) is desired


def test_apply_heartbeat_reconciles_desired_worker_units_and_signins(monkeypatch):
    state = agent._AgentLoopState(tools={}, tools_at=0)
    desired = {"workers": ["codex-new"], "accounts": ["ignored"],
               "project_environment": {"DEMO": ["API_KEY"]},
               "sign_ins": [{"request_id": "one"}], "signin": [{"request_id": "two"}]}
    calls = []
    sync_requests = []

    def systemctl(*args):
        calls.append(args)
        return 3 if args[:2] == ("is-active", "--quiet") else 0

    monkeypatch.setattr(agent, "_apply_toolchains", lambda *_args: None)
    monkeypatch.setattr(agent, "_apply_services", lambda *_args: None)
    monkeypatch.setattr(agent, "_apply_probes", lambda *_args: None)
    monkeypatch.setattr(agent, "_apply_queued_work", lambda *_args: None)
    monkeypatch.setattr(agent, "sync_provider_sign_ins",
                        lambda _api, _node, requests, _processes: sync_requests.extend(requests))
    monkeypatch.setattr(agent, "systemctl", systemctl)
    monkeypatch.setattr(agent, "ensure_account_home", lambda _account: True)
    monkeypatch.setattr(agent.subprocess, "run", lambda *args, **_kwargs:
                        SimpleNamespace(stdout="aiflow4-worker@codex-old.service loaded active running\n"))
    monkeypatch.setattr(agent, "clean_scratch", lambda: None)
    monkeypatch.setattr(agent, "clean_worktrees", lambda *_args: None)
    monkeypatch.setattr(agent, "sweep_test_databases", lambda: None)
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")

    assert agent.apply_heartbeat(object(), "node-test", desired, state, True, threading.Event(), 15)

    assert state.project_environment == {"DEMO": ["API_KEY"]}
    assert sync_requests == [{"request_id": "one"}, {"request_id": "two"}]
    assert calls == [
        ("is-active", "--quiet", "aiflow4-worker@codex-new"),
        ("enable", "--now", "aiflow4-worker@codex-new"),
        ("disable", "--now", "aiflow4-worker@codex-old.service"),
    ]


def test_heartbeat_carries_the_version_or_missing_for_each_project_tool(monkeypatch):
    heartbeats = []
    stop = threading.Event()

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, path, body=None, **kwargs):
            heartbeats.append(body)
            stop.set()
            return {"desired": {"accounts": [], "workers": [], "projects": ["TESTWP"],
                                "tools": ["php", "composer", "wp"]}}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "wp":
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} version\n", stderr="")

    monkeypatch.setattr(agent, "ControlClient", FakeApi)
    monkeypatch.setattr(agent, "metrics", lambda prev: ({"hostname": "node1"}, (1, 1)))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")

    agent.run_agent("http://control", "node1", "token", every_s=0, stop=stop, manage_workers=False)

    tools = heartbeats[0]["tools"]
    assert tools["php"] == "php version"
    assert tools["composer"] == "composer version"
    assert tools["wp"] == "missing"


def test_node_heartbeat_reports_policy_relay_health(monkeypatch):
    class FakeRelay:
        def __init__(self, control_url):
            assert control_url == "http://control.test"
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True
            return True

        def health(self):
            return {"enabled": True, "healthy": self.started and not self.stopped}

        def stop(self):
            self.stopped = True

    class FakeControlClient:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def call(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"desired": {"workers": [], "release": ""}}

    class StopAfterHeartbeat:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    relays = []
    clients = []

    def fake_relay(control_url):
        relay = FakeRelay(control_url)
        relays.append(relay)
        return relay

    def fake_client(*args, **kwargs):
        client = FakeControlClient(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "1")
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")
    monkeypatch.setattr(agent, "PolicyRelay", fake_relay)
    monkeypatch.setattr(agent, "ControlClient", fake_client)
    monkeypatch.setattr(agent, "tool_versions", lambda: {})
    monkeypatch.setattr(agent, "metrics", lambda _previous: ({"cpu_pct": 0}, (1, 1)))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent, "installed_release", lambda: "")

    agent.run_agent("http://control.test", "node1", "token", manage_workers=False, stop=StopAfterHeartbeat())

    heartbeat = clients[0].calls[0][0][2]
    assert heartbeat["metrics"]["policy_relay"] == {"enabled": True, "healthy": True}
    assert heartbeat["release_protocol"] == 1
    assert relays[0].started and relays[0].stopped


def test_node_agent_reports_cli_expiry_and_queued_heartbeat_retry(monkeypatch):
    expiry = "2026-10-25T10:00:00+00:00"
    state = {"account": "claude-01", "state": "idle", "probe": {"credential_expires_at": expiry}}
    assert agent.credential_expiries([state, {"account": "codex-01", "probe": {"credential_expires_at": None}}]) == {
        "claude-01": expiry, "codex-01": None}

    class FakeApi:
        def __init__(self, *args, **kwargs):
            self.heartbeats = 0
            self.event_batches = []
            self.calls = []

        def call(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body))
            if path.endswith("/heartbeat"):
                self.heartbeats += 1
                if self.heartbeats == 1:
                    raise RuntimeError("control API unreachable: timeout")
                self.heartbeat_body = body
                return {"desired": {"workers": [], "release": ""}}
            if path == "/v1/events":
                self.event_batches.append(body)
                return {"accepted": len(body["events"])}
            if path.endswith("/operations/next"):
                return {"operation": None}
            raise AssertionError(path)

    class StopAfterRecovery:
        waits = 0

        def is_set(self):
            return self.waits >= 2

        def wait(self, _seconds):
            self.waits += 1

    clients = []

    def make_client(*args, **kwargs):
        api = FakeApi()
        clients.append(api)
        return api

    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")
    monkeypatch.setattr(agent, "ControlClient", make_client)
    monkeypatch.setattr(agent, "tool_versions", lambda: {})
    monkeypatch.setattr(agent, "metrics", lambda _previous: ({"cpu_pct": 0}, (1, 1)))
    monkeypatch.setattr(agent, "worker_states", lambda: [state])
    monkeypatch.setattr(agent, "installed_release", lambda: "")

    agent.run_agent("http://control.test", "node1", "token", every_s=0, manage_workers=False,
                    stop=StopAfterRecovery())

    assert clients[0].heartbeat_body["credential_expiries"] == {"claude-01": expiry}
    assert [path for _, path, _ in clients[0].calls] == ["/v1/nodes/node1/heartbeat", "/v1/nodes/node1/heartbeat",
                                                       "/v1/events", "/v1/nodes/node1/operations/next"]
    event = clients[0].event_batches[0]["events"][0]
    assert event["type"] == "node.heartbeat.error" and event["error_kind"] == "network"
    assert event["retry"] is True and event["attempts"] == 2


def test_a_slow_tool_check_is_not_reported_missing(monkeypatch):
    import subprocess as sp

    def fake_run(cmd, **kwargs):
        if cmd[0] == "composer":
            raise sp.TimeoutExpired(cmd, 30)
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} 1.0\n", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    from aiflow.v4.core.toolchains import missing_tools
    versions = agent.tool_versions()
    assert versions["composer"] == "timeout" and missing_tools(versions, ["composer"]) == []


class _ReleaseResponse:
    status_code = 200

    def __init__(self, content: bytes, payload: dict | None = None, status_code: int = 200):
        self.content = content
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def test_self_update_rejects_bad_signature_reports_incident_and_does_not_install(tmp_path, monkeypatch):
    import hashlib

    version = "v4.2.1"
    tarball = b"signed archive bytes"
    bundle = b"untrusted signature bundle"
    manifest = {"version": version, "sha256": hashlib.sha256(tarball).hexdigest(),
                "tarball": f"/v1/releases/{version}.tar.gz",
                "signature": f"/v1/releases/{version}.tar.gz.sigstore.json"}

    class FakeHTTP:
        def get(self, url, timeout):
            if url == f"/v1/releases/{version}/manifest":
                return _ReleaseResponse(b"", manifest)
            if url == manifest["tarball"]:
                return _ReleaseResponse(tarball)
            if url == manifest["signature"]:
                return _ReleaseResponse(bundle)
            raise AssertionError(f"unexpected release URL: {url}")

    class FakeAPI:
        def __init__(self):
            self.c = FakeHTTP()
            self.calls = []

        def call(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body, kwargs))

    api = FakeAPI()
    subprocess_calls = []
    monkeypatch.setattr(agent, "RELEASE_INCOMING_DIR", tmp_path)
    monkeypatch.setattr(agent, "installed_release", lambda: "")
    monkeypatch.setattr(agent, "verify_release_signature", lambda *_: (False, "invalid signature"))
    monkeypatch.setattr(agent.subprocess, "run", lambda *args, **kwargs: subprocess_calls.append(args[0]))
    agent._update_tried.clear()

    agent.self_update(api, version, "node-7")

    assert api.calls == [("POST", "/v1/nodes/node-7/release-failures",
                          {"release_version": version,
                           "detail": "cosign signature verification failed: invalid signature"},
                          {"retries": 1})]
    assert not subprocess_calls


def test_self_update_reports_unavailable_manifest_or_signature_as_verification_failure(tmp_path, monkeypatch):
    import hashlib

    subprocess_calls = []
    monkeypatch.setattr(agent, "RELEASE_INCOMING_DIR", tmp_path)
    monkeypatch.setattr(agent, "installed_release", lambda: "")
    monkeypatch.setattr(agent.subprocess, "run", lambda *args, **kwargs: subprocess_calls.append(args[0]))

    for version, missing_asset, expected_detail in (
        ("v4.2.2", "manifest", "release manifest unavailable (HTTP 404)"),
        ("v4.2.3", "signature", "release signature bundle unavailable (HTTP 404)"),
    ):
        tarball = b"signed archive bytes"
        manifest = {"version": version, "sha256": hashlib.sha256(tarball).hexdigest(),
                    "tarball": f"/v1/releases/{version}.tar.gz",
                    "signature": f"/v1/releases/{version}.tar.gz.sigstore.json"}

        class FakeHTTP:
            def get(self, url, timeout):
                if url == f"/v1/releases/{version}/manifest":
                    if missing_asset == "manifest":
                        return _ReleaseResponse(b"", status_code=404)
                    return _ReleaseResponse(b"", manifest)
                if url == manifest["tarball"]:
                    return _ReleaseResponse(tarball)
                if url == manifest["signature"]:
                    if missing_asset == "signature":
                        return _ReleaseResponse(b"", status_code=404)
                    return _ReleaseResponse(b"signature bundle")
                raise AssertionError(f"unexpected release URL: {url}")

        class FakeAPI:
            def __init__(self):
                self.c = FakeHTTP()
                self.calls = []

            def call(self, method, path, body=None, **kwargs):
                self.calls.append((method, path, body, kwargs))

        api = FakeAPI()
        agent._update_tried.clear()

        agent.self_update(api, version, "node-7")

        assert api.calls == [("POST", "/v1/nodes/node-7/release-failures",
                              {"release_version": version, "detail": expected_detail}, {"retries": 1})]
        assert not (tmp_path / f"aiflow4-{version}.tar.gz").exists()

    assert not subprocess_calls


def test_self_update_schedules_install_only_after_checksum_and_signature_pass(tmp_path, monkeypatch):
    import hashlib

    version = "v4.2.1"
    tarball = b"verified archive bytes"
    bundle = b"verified signature bundle"
    manifest = {"version": version, "sha256": hashlib.sha256(tarball).hexdigest(),
                "tarball": f"/v1/releases/{version}.tar.gz",
                "signature": f"/v1/releases/{version}.tar.gz.sigstore.json"}

    class FakeHTTP:
        def get(self, url, timeout):
            return {f"/v1/releases/{version}/manifest": _ReleaseResponse(b"", manifest),
                    manifest["tarball"]: _ReleaseResponse(tarball),
                    manifest["signature"]: _ReleaseResponse(bundle)}[url]

    class FakeAPI:
        c = FakeHTTP()

        def __init__(self):
            self.calls = []

        def call(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    api = FakeAPI()
    commands = []
    monkeypatch.setattr(agent, "RELEASE_INCOMING_DIR", tmp_path)
    monkeypatch.setattr(agent, "installed_release", lambda: "")
    monkeypatch.setattr(agent, "verify_release_signature", lambda *_: (True, ""))
    monkeypatch.setattr(agent.subprocess, "run", lambda cmd, **kwargs: commands.append(cmd)
                        or SimpleNamespace(returncode=0, stdout="started", stderr=""))
    agent._update_tried.clear()

    agent.self_update(api, version, "node-7")

    assert not api.calls
    assert commands[0][0:5] == ["systemd-run", "--unit", commands[0][2], "--no-block", "--collect"]
    assert commands[0][5] == "/usr/local/sbin/aiflow4-node-install"
    assert commands[0][7] == version
    assert commands[0][9] == manifest["sha256"]


def test_release_signature_verification_pins_the_tagged_workflow_identity(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(agent.subprocess, "run", lambda cmd, **kwargs: commands.append(cmd)
                        or SimpleNamespace(returncode=0, stdout="verified", stderr=""))

    assert agent.verify_release_signature(tmp_path / "release.tar.gz", tmp_path / "bundle.json", "v4.2.1") == (True, "")
    assert commands[0][0:4] == ["cosign", "verify-blob", str(tmp_path / "release.tar.gz"), "--bundle"]
    assert commands[0][4] == str(tmp_path / "bundle.json")
    assert commands[0][5:7] == ["--certificate-identity", agent._RELEASE_WORKFLOW_IDENTITY.format("v4.2.1")]
    assert commands[0][7:9] == ["--certificate-oidc-issuer", "https://token.actions.githubusercontent.com"]

    commands.clear()
    assert agent.verify_release_signature(tmp_path / "release.tar.gz", tmp_path / "bundle.json", "develop") == (
        False, "release version is not a vX.Y.Z tag")
    assert not commands


def test_node_removes_the_worktrees_the_control_plane_marks_finished(tmp_path, monkeypatch):
    import os
    import time
    from aiflow.v4.node import agent

    work = tmp_path / "work" / "TESTWP"
    for name in ("TESTWP-1-done", "TESTWP-2-open", "TESTWP-2--intake"):
        (work / name / "node_modules").mkdir(parents=True)
        os.utime(work / name, (time.time() - 3600,) * 2)
    ro = work / "TESTWP-2--intake"
    os.chmod(ro, 0o500)

    class Api:
        def call(self, method, path, body=None, retries=0):
            assert path == "/v1/nodes/cc/worktrees/sweep"
            assert sorted(body["dirs"]) == ["TESTWP/TESTWP-1-done", "TESTWP/TESTWP-2--intake", "TESTWP/TESTWP-2-open"]
            return {"remove": ["TESTWP/TESTWP-1-done", "TESTWP/TESTWP-2--intake", "TESTWP/not-listed"]}

    monkeypatch.setattr(agent, "_last_sweep", 0.0)
    assert agent.clean_worktrees(Api(), "cc", str(tmp_path)) == 2
    assert [p.name for p in work.iterdir()] == ["TESTWP-2-open"]
    assert agent.clean_worktrees(Api(), "cc", str(tmp_path)) == 0  # at most every ten minutes


def test_node_clears_e2e_artifacts_in_a_root_container_before_removing_worktree(tmp_path, monkeypatch):
    import shutil
    import time

    worktree = tmp_path / "work" / "TESTWP" / "TESTWP-1-done"
    e2e = worktree / "artifacts" / "reports" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "report.json").write_text("generated by e2e")
    os.utime(worktree, (time.time() - 3600,) * 2)

    class Api:
        def call(self, method, path, body=None, retries=0):
            assert path == "/v1/nodes/cc/worktrees/sweep"
            assert body["dirs"] == ["TESTWP/TESTWP-1-done"]
            return {"remove": ["TESTWP/TESTWP-1-done"]}

    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        assert command[:2] == ["docker", "run"]
        assert "--user=0:0" in command
        assert "--network=none" in command and "--read-only" in command
        assert any(f"source={worktree.resolve()},target=/worktree" in arg for arg in command)
        assert command[-4:] == ["rm", "-rf", "--", "/worktree/artifacts"]
        shutil.rmtree(worktree / "artifacts")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(agent, "_last_sweep", 0.0)
    monkeypatch.setattr(agent.subprocess, "run", fake_run)

    assert agent.clean_worktrees(Api(), "cc", str(tmp_path)) == 1
    assert len(commands) == 1 and commands[0][1]["timeout"] == 120
    assert not worktree.exists()


def test_node_reports_worktree_and_reason_when_root_cleanup_cannot_remove_e2e_entry(tmp_path, monkeypatch, caplog):
    import time

    worktree = tmp_path / "work" / "TESTWP" / "TESTWP-2-done"
    e2e = worktree / "artifacts" / "reports" / "e2e"
    e2e.mkdir(parents=True)
    undeletable = e2e / "locked-report.json"
    undeletable.write_text("root-owned e2e output")
    os.chmod(e2e, 0o555)
    os.utime(worktree, (time.time() - 3600,) * 2)

    class Api:
        def call(self, method, path, body=None, retries=0):
            assert path == "/v1/nodes/cc/worktrees/sweep"
            assert body["dirs"] == ["TESTWP/TESTWP-2-done"]
            return {"remove": ["TESTWP/TESTWP-2-done"]}

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="Permission denied: locked-report.json")

    monkeypatch.setattr(agent, "_last_sweep", 0.0)
    monkeypatch.setattr(agent.subprocess, "run", fake_run)

    assert agent.clean_worktrees(Api(), "cc", str(tmp_path)) == 0
    assert len(commands) == 1 and commands[0][:2] == ["docker", "run"]
    assert worktree.is_dir() and undeletable.is_file()
    assert "TESTWP/TESTWP-2-done" in caplog.text
    assert "Permission denied: locked-report.json" in caplog.text
    os.chmod(e2e, 0o700)


def test_develop_channel_installs_a_commit_build_only_when_the_node_opted_in(tmp_path, monkeypatch):
    sha = "0123456789"

    class FakeHTTP:
        def get(self, url, timeout):
            assert url == f"/v1/releases/{sha}.tar.gz"
            return _ReleaseResponse(b"develop archive")

    class FakeAPI:
        c = FakeHTTP()

    commands = []
    monkeypatch.setattr(agent, "RELEASE_INCOMING_DIR", tmp_path)
    monkeypatch.setattr(agent, "installed_release", lambda: "")
    monkeypatch.setattr(agent.subprocess, "run", lambda cmd, **kwargs: commands.append(cmd)
                        or SimpleNamespace(returncode=0, stdout="started", stderr=""))

    monkeypatch.delenv("AIFLOW4_CHANNEL", raising=False)
    agent._update_tried.clear()
    agent.self_update(FakeAPI(), sha, "node-7")
    assert not commands  # a release-channel node ignores unversioned builds

    monkeypatch.setenv("AIFLOW4_CHANNEL", "develop")
    agent._update_tried.clear()
    agent.self_update(FakeAPI(), sha, "node-7")
    assert commands[0][0] == "systemd-run" and commands[0][5] == "/usr/local/sbin/aiflow4-develop-install"
    assert commands[0][6] == str(tmp_path / f"aiflow4-{sha}.tar.gz") and commands[0][7] == sha
    assert (tmp_path / f"aiflow4-{sha}.tar.gz").read_bytes() == b"develop archive"
