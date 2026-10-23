"""Safe container broker command, path, cache and storage policy tests."""
from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.worker import containers


def _fake_runtime(monkeypatch, tmp_path: Path, *, output: str = "ok") -> list[list[str]]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    executable = bin_dir / "podman"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys\n"
        f"pathlib.Path({str(calls)!r}).open('a').write(' '.join(sys.argv[1:])+'\\n')\n"
        f"print({output!r})\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
    monkeypatch.setattr(containers, "_graphroot", lambda: tmp_path)
    monkeypatch.setattr(containers, "_directory_size", lambda path: 0)
    monkeypatch.setattr(containers.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024**3))
    monkeypatch.setattr(containers, "_active_containers", lambda: [])
    monkeypatch.setattr(containers.Path, "home", lambda: tmp_path)
    return calls


def test_broker_builds_and_runs_through_rootless_podman_with_layer_limits(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / "Containerfile").write_text("FROM scratch\n")
    (worktree / "marker").write_text("for this checkout")
    calls = _fake_runtime(monkeypatch, tmp_path)

    code, stdout, _ = containers._execute(["build", "-t", "sample", "."], worktree, worktree,
                                           runtime_dir=tmp_path, project_name="aiflow-test")
    assert code == 0 and "ok" in stdout
    code, stdout, _ = containers._execute(["run", "--volume", f"{worktree}:/workspace", "alpine",
                                           "cat", "/workspace/marker"], worktree, worktree,
                                           runtime_dir=tmp_path, project_name="aiflow-test")
    assert code == 0 and "ok" in stdout
    commands = calls.read_text().splitlines()
    assert commands[0].startswith(f"build --storage-opt size=2g -t sample {worktree}")
    assert commands[1].startswith("run --label aiflow4.run=aiflow-test --storage-opt size=1g --rm --volume ")


def test_broker_launch_uses_installed_code_and_excludes_the_worktree_from_imports(tmp_path):
    worktree = tmp_path / "checkout"
    engine_home = tmp_path / "engine-home"
    (worktree / "aiflow" / "v4" / "worker").mkdir(parents=True)
    engine_home.mkdir()
    (worktree / "aiflow" / "__init__.py").write_text("raise RuntimeError('untrusted project package imported')\n")
    expected = Path(containers.__file__).resolve()

    argv, cwd = containers._broker_invocation(worktree, str(engine_home))
    assert argv[:2] == [sys.executable, "-I"]
    assert "-m" not in argv
    assert cwd == str(engine_home.resolve())
    bootstrap = argv[3].replace("raise SystemExit(module._broker_main())", "print(module.__file__)")
    result = subprocess.run([*argv[:3], bootstrap], cwd=worktree, capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()).resolve() == expected


def test_broker_launch_refuses_code_loaded_from_the_project_worktree(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    module_file = worktree / "aiflow" / "v4" / "worker" / "containers.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("# untrusted\n")
    monkeypatch.setattr(containers, "__file__", str(module_file))
    with pytest.raises(RuntimeError, match="installed outside"):
        containers._broker_invocation(worktree, str(tmp_path))


def test_compose_up_and_run_validate_files_and_add_storage_caps(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / "src").mkdir()
    compose = worktree / "compose.yaml"
    compose.write_text("""services:
  composer:
    image: composer:2
    volumes:
      - ./src:/worktree
      - vendor:/vendor
volumes:
  vendor: {}
""")
    monkeypatch.setattr(containers, "_active_containers", lambda: [])
    monkeypatch.setattr(containers, "_engine_identity", lambda: (os.getuid(), os.getgid(), str(tmp_path)))
    up, _, _ = containers._compose_command(["compose", "up", "-d"], worktree, worktree, tmp_path, "aiflow-run")
    assert up[-3:] == ["-p", "aiflow-run", "up", "-d"][-3:]
    file_flags = [index for index, item in enumerate(up) if item == "-f"]
    override = Path(up[file_flags[-1] + 1])
    override_data = __import__("yaml").safe_load(override.read_text())
    assert override_data["services"]["composer"]["storage_opt"]["size"] == "1g"
    # Podman's size= volume option needs root; rootless volumes must not ask for it.
    assert "volumes" not in override_data
    run, _, _ = containers._compose_command(["compose", "run", "composer", "composer", "ci"],
                                             worktree, worktree, tmp_path, "aiflow-run")
    assert run[run.index("run") + 1:run.index("run") + 3] == ["--rm", "composer"]


def test_direct_run_cannot_mount_host_paths_or_another_worktree(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    secrets = tmp_path / "secrets"
    other = tmp_path / "other-account"
    secrets.mkdir()
    other.mkdir()
    for mount in ("/:/host", f"{secrets}:/secrets", f"{other}:/other", "../other-account:/other"):
        with pytest.raises(containers.ContainerPolicyError):
            containers._run_argv(["run", "-v", mount, "alpine", "true"], worktree, worktree)
    for network in ("host", "container:other-run", "aiflow-shared"):
        with pytest.raises(containers.ContainerPolicyError):
            containers._run_argv(["run", "--network", network, "-v", f"{worktree}:/worktree",
                                  "alpine", "true"], worktree, worktree)
    with pytest.raises(containers.ContainerPolicyError, match="label is reserved"):
        containers._run_argv(["run", "--label", "aiflow4.run=other-run", "-v", f"{worktree}:/worktree",
                              "alpine", "true"], worktree, worktree)


def test_symlinked_worktree_path_cannot_escape_to_another_account(tmp_path):
    worktree = tmp_path / "checkout"
    other = tmp_path / "other-account"
    worktree.mkdir()
    other.mkdir()
    (worktree / "escape").symlink_to(other, target_is_directory=True)
    with pytest.raises(containers.ContainerPolicyError):
        containers._run_argv(["run", "-v", f"{worktree / 'escape'}:/other", "alpine", "true"],
                             worktree, worktree)


def test_compose_cannot_escalate_or_mount_outside_worktree(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    secrets = tmp_path / "secrets"
    other = tmp_path / "other-account"
    secrets.mkdir()
    other.mkdir()
    compose = worktree / "compose.yaml"
    unsafe = ["    privileged: true\n", "    pid: host\n", "    cap_add: [SYS_ADMIN]\n",
              "    volumes:\n      - /:/host\n",
              f"    volumes:\n      - {secrets}:/secrets\n",
              f"    volumes:\n      - {other}:/other\n",
              "    labels:\n      aiflow4.run: other-run\n",
              "    extends:\n      file: ../other-compose.yaml\n      service: base\n"]
    for service in unsafe:
        compose.write_text("services:\n  test:\n    image: alpine\n" + service)
        data = __import__("yaml").safe_load(compose.read_text())
        with pytest.raises(containers.ContainerPolicyError):
            containers._validate_compose_document(data, compose, worktree, worktree)


def test_compose_rejects_anonymous_unbounded_volumes(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    compose = worktree / "compose.yaml"
    compose.write_text("services:\n  test:\n    image: alpine\n    volumes:\n      - /cache\n")
    import yaml
    with pytest.raises(containers.ContainerPolicyError, match="anonymous compose volumes"):
        containers._validate_compose_document(yaml.safe_load(compose.read_text()), compose, worktree, worktree)


def test_compose_cannot_join_external_or_custom_networks(tmp_path):
    import yaml

    worktree = tmp_path / "checkout"
    worktree.mkdir()
    compose = worktree / "compose.yaml"
    for networks in ("    external: true\n", "    name: shared-network\n", "    driver: host\n",
                     "    driver_opts:\n      device: /\n"):
        compose.write_text("services:\n  test:\n    image: alpine\nnetworks:\n  default:\n" + networks)
        with pytest.raises(containers.ContainerPolicyError):
            containers._validate_compose_document(yaml.safe_load(compose.read_text()), compose, worktree, worktree)


def test_compose_run_rejects_privileged_overrides(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / "compose.yaml").write_text("services:\n  test:\n    image: alpine\n")
    for option in ("--privileged", "--cap-add SYS_ADMIN", "--device /dev/kvm", "--network host",
                   "--volume /:/host"):
        argv = ["compose", "run", *option.split(), "test", "true"]
        with pytest.raises(containers.ContainerPolicyError):
            containers._compose_command(argv, worktree, worktree, tmp_path, "aiflow-run")


def test_cache_ceiling_refuses_new_build_and_old_images_are_pruned(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(containers.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(containers, "_graphroot", lambda: tmp_path)
    monkeypatch.setattr(containers, "_directory_size", lambda path: containers.IMAGE_CACHE_LIMIT)
    monkeypatch.setattr(containers, "_active_containers", lambda: [])
    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    with pytest.raises(containers.ContainerPolicyError, match="cache"):
        containers._execute(["build", "."], worktree, worktree, runtime_dir=tmp_path, project_name="aiflow-test")
    assert calls[0][:4] == ["podman", "image", "prune", "--all"]
    assert "until=168h" in calls[0]

    prunes: list[list[str]] = []
    monkeypatch.setattr(containers, "safe_container_runtime_version", lambda: "rootless Podman 5")
    monkeypatch.setattr(containers, "_run_as_engine", lambda argv, **kwargs:
                        prunes.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert containers.prune_image_cache()
    assert prunes == [["podman", "container", "prune", "--filter", "until=24h", "--force"],
                      ["podman", "image", "prune", "--all", "--filter", "until=168h", "--force"]]


def test_runtime_preflight_requires_rootless_podman_compose_project_quota_and_capacity(monkeypatch, tmp_path):
    graphroot = tmp_path / "storage"
    graphroot.mkdir()
    info_data = {"host": {"security": {"rootless": True}}, "store": {"graphRoot": str(graphroot)},
                 "version": {"Version": "5.4.0"}}

    def worker(argv, **kwargs):
        if argv[:2] == ["podman", "info"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(info_data), "")
        if argv[:3] == ["podman", "unshare", "true"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "podman-compose version 1.2.0", "")

    monkeypatch.setattr(containers, "_run_as_engine", worker)
    monkeypatch.setattr(containers, "_identity",
                        lambda user: (1001 if user == containers.WORKER_USER else 1002, 1, str(graphroot)))
    monkeypatch.setattr(containers, "_worker_can_run_podman", lambda: False)
    monkeypatch.setattr(containers, "_private_to_engine", lambda path: True)
    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "xfs rw,relatime,pquota", ""))
    monkeypatch.setattr(containers.shutil, "disk_usage", lambda path:
                        SimpleNamespace(total=30 * 1024**3, free=10 * 1024**3))
    assert containers.safe_container_runtime_version().startswith("rootless Podman 5.4.0")

    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "xfs rw,relatime", ""))
    assert containers.safe_container_runtime_version() == "missing"

    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "ext4 rw,relatime,pquota", ""))
    assert containers.safe_container_runtime_version() == "missing"

    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "xfs rw,relatime,pquota", ""))
    assert containers.safe_container_runtime_version().startswith("rootless Podman")
    monkeypatch.setattr(containers, "_private_to_engine", lambda path: False)
    assert containers.safe_container_runtime_version() == "missing"
    monkeypatch.setattr(containers, "_private_to_engine", lambda path: True)

    info_data["host"]["security"]["rootless"] = False
    assert containers.safe_container_runtime_version() == "missing"


def test_compose_includes_and_external_volumes_are_denied(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    compose = worktree / "compose.yaml"
    compose.write_text("""include: ../other-compose.yaml
services:
  test:
    image: alpine
""")
    import yaml
    with pytest.raises(containers.ContainerPolicyError, match="include"):
        containers._validate_compose_document(yaml.safe_load(compose.read_text()), compose, worktree, worktree)


def test_runtime_keeps_disk_reserve_when_the_container_filesystem_is_nearly_full(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    monkeypatch.setattr(containers.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(containers, "_graphroot", lambda: tmp_path)
    monkeypatch.setattr(containers, "_directory_size", lambda path: 0)
    monkeypatch.setattr(containers, "_active_containers", lambda: [])
    monkeypatch.setattr(containers.shutil, "disk_usage", lambda path:
                        SimpleNamespace(free=4 * 1024**3, total=100 * 1024**3))
    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(containers.subprocess, "Popen", lambda *args, **kwargs:
                        pytest.fail("runtime must refuse before starting Podman"))
    with pytest.raises(containers.ContainerPolicyError, match="disk reserve"):
        containers._execute(["build", "."], worktree, worktree, runtime_dir=tmp_path, project_name="aiflow-test")


def test_runtime_refuses_a_worktree_that_reached_its_storage_limit(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    monkeypatch.setattr(containers.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(containers, "_graphroot", lambda: tmp_path)
    monkeypatch.setattr(containers, "_directory_size", lambda path: 0)
    monkeypatch.setattr(containers, "_worktree_storage_size", lambda path: containers.WORKTREE_STORAGE_LIMIT)
    monkeypatch.setattr(containers, "_active_containers", lambda: [])
    monkeypatch.setattr(containers.shutil, "disk_usage", lambda path:
                        SimpleNamespace(free=100 * 1024**3, total=120 * 1024**3))
    monkeypatch.setattr(containers.subprocess, "Popen", lambda *args, **kwargs:
                        pytest.fail("runtime must refuse before starting Podman"))
    with pytest.raises(containers.ContainerPolicyError, match="worktree reached"):
        containers._execute(["build", "."], worktree, worktree, runtime_dir=tmp_path, project_name="aiflow-test")


def test_runtime_stops_a_container_that_exceeds_its_worktree_storage_limit(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    monkeypatch.setattr(containers.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(containers, "_graphroot", lambda: tmp_path)
    monkeypatch.setattr(containers, "_directory_size", lambda path: 0)
    usage = iter((0, containers.WORKTREE_STORAGE_LIMIT))
    monkeypatch.setattr(containers, "_worktree_storage_size", lambda path: next(usage))
    monkeypatch.setattr(containers, "_active_containers", lambda: [])
    monkeypatch.setattr(containers.shutil, "disk_usage", lambda path:
                        SimpleNamespace(free=100 * 1024**3, total=120 * 1024**3))
    monkeypatch.setattr(containers.os, "killpg", lambda *args: None)
    stopped: list[str] = []
    monkeypatch.setattr(containers, "_stop_run_containers", lambda name: stopped.append(name))

    class RunningProcess:
        pid = 12345
        returncode = None

        def __init__(self):
            read_fd, write_fd = os.pipe()
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

    monkeypatch.setattr(containers.subprocess, "Popen", lambda *args, **kwargs: RunningProcess())
    code, output, _ = containers._execute(["build", "."], worktree, worktree,
                                           runtime_dir=tmp_path, project_name="aiflow-test")
    assert code == 125
    assert "worktree storage cap reached" in output
    assert stopped == ["aiflow-test"]


def test_compose_cleanup_is_idempotent_and_orphan_watcher_cleans_up(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kwargs:
                        calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    server = containers._BrokerServer(str(tmp_path / "broker.sock"), tmp_path, tmp_path, "aiflow-run", "token")
    try:
        server.compose_used = True
        server.cleanup()
        server.cleanup()
        assert len(calls) == 3
    finally:
        server.server_close()

    class ImmediateEvent:
        def __init__(self, state=False):
            self.state = state

        def wait(self, timeout=None):
            return self.state

        def is_set(self):
            return self.state

        def set(self):
            self.state = True

    class FakeServer:
        allowed_root_pid = 77
        run_start_time = 123
        bound_event = ImmediateEvent(True)
        stop_requested = ImmediateEvent()
        cancel_commands = ImmediateEvent()
        commands_idle = ImmediateEvent(True)

        def cleanup(self):
            self.cleaned = True

        def shutdown(self):
            self.stopped = True

    fake = FakeServer()
    monkeypatch.setattr(containers, "_proc_identity", lambda pid: None)
    containers._watch_run(fake)
    assert fake.cancel_commands.is_set() and fake.cleaned and fake.stopped


def test_interrupted_direct_run_cleanup_is_scoped_to_its_run_label(monkeypatch):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        output = "container-one\ncontainer-two\n" if argv[:3] == ["podman", "ps", "--all"] else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(containers.subprocess, "run", run)
    containers._stop_run_containers("aiflow-test")
    assert calls == [
        ["podman", "ps", "--all", "--quiet", "--filter", "label=aiflow4.run=aiflow-test"],
        ["podman", "stop", "--time", "1", "container-one", "container-two"],
        ["podman", "rm", "--force", "--volumes", "container-one", "container-two"],
    ]


def test_run_watcher_stops_detached_compose_when_worktree_storage_limit_is_reached(monkeypatch, tmp_path):
    class ImmediateEvent:
        def __init__(self, state=False):
            self.state = state

        def wait(self, timeout=None):
            return self.state

        def is_set(self):
            return self.state

        def set(self):
            self.state = True

    class StopAfterOnePoll(ImmediateEvent):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls > 1:
                self.state = True
            return self.state

    class FakeServer:
        worktree = tmp_path
        allowed_root_pid = 77
        run_start_time = 123
        compose_used = True
        bound_event = ImmediateEvent(True)
        stop_requested = StopAfterOnePoll()
        cancel_commands = ImmediateEvent()
        storage_exceeded = ImmediateEvent()
        commands_idle = ImmediateEvent(True)

        def cleanup(self):
            self.cleaned = True

        def shutdown(self):
            self.stopped = True

    fake = FakeServer()
    monkeypatch.setattr(containers, "_proc_identity", lambda pid: 123)
    monkeypatch.setattr(containers, "_worktree_storage_limit_reached", lambda path: True)
    containers._watch_run(fake)
    assert fake.storage_exceeded.is_set()
    assert fake.cancel_commands.is_set() and fake.cleaned


def test_run_watcher_keeps_enforcing_storage_after_detached_compose_up(monkeypatch, tmp_path):
    class ImmediateEvent:
        def __init__(self, state=False):
            self.state = state

        def wait(self, timeout=None):
            return self.state

        def is_set(self):
            return self.state

        def set(self):
            self.state = True

    class StopAfterOnePoll(ImmediateEvent):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls > 1:
                self.state = True
            return self.state

    class FakeServer:
        worktree = tmp_path
        allowed_root_pid = 77
        run_start_time = 123
        compose_used = True
        bound_event = ImmediateEvent(True)
        stop_requested = StopAfterOnePoll()
        cancel_commands = ImmediateEvent()
        storage_exceeded = ImmediateEvent()
        commands_idle = ImmediateEvent(True)

        def cleanup(self):
            self.cleaned = True

        def shutdown(self):
            self.stopped = True

    fake = FakeServer()
    monkeypatch.setattr(containers, "_proc_identity", lambda pid: 123)
    monkeypatch.setattr(containers, "_worktree_storage_limit_reached", lambda path: False)
    monkeypatch.setattr(containers, "_runtime_storage_limit_reached", lambda: True)
    containers._watch_run(fake)
    assert fake.storage_exceeded.is_set()
    assert fake.cancel_commands.is_set() and fake.cleaned


def test_compose_custom_volume_names_are_rejected(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    compose = worktree / "compose.yaml"
    compose.write_text("services:\n  app:\n    image: alpine\n    volumes:\n      - data:/data\n"
                       "volumes:\n  data:\n    name: another-runs-volume\n")
    with pytest.raises(containers.ContainerPolicyError, match="custom compose volume names"):
        containers._compose_command(["compose", "up"], worktree, worktree, tmp_path, "aiflow-test")


def test_runtime_is_missing_when_the_run_account_can_execute_podman(monkeypatch, tmp_path):
    monkeypatch.setattr(containers, "_identity",
                        lambda user: (1001 if user == containers.WORKER_USER else 1002, 1, str(tmp_path)))
    monkeypatch.setattr(containers, "_worker_can_run_podman", lambda: True)
    assert containers.safe_container_runtime_version() == "missing"
    # The same account for the run and the engine would make the broker pointless.
    monkeypatch.setattr(containers, "_worker_can_run_podman", lambda: False)
    monkeypatch.setattr(containers, "_identity", lambda user: (1001, 1, str(tmp_path)))
    assert containers.safe_container_runtime_version() == "missing"


def test_broker_startup_as_engine_user_checks_engine_health_without_worker_uid_switch(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    worktree_info = worktree.stat()
    socket_path = tmp_path / "broker.sock"
    socket_path.write_text("")
    engine_uid, worker_uid = 1002, 1001
    monkeypatch.setattr(containers.sys, "argv", ["containers.py", "--broker", "--socket", str(socket_path),
                        "--worktree", str(worktree), "--worktree-device", str(worktree_info.st_dev),
                        "--worktree-inode", str(worktree_info.st_ino), "--runtime-dir", str(tmp_path),
                        "--project", "aiflow-test", "--token", "token"])
    monkeypatch.setattr(containers.os, "geteuid", lambda: engine_uid)
    monkeypatch.setattr(containers, "_engine_identity", lambda: (engine_uid, engine_uid, str(tmp_path)))
    monkeypatch.setattr(containers, "_identity", lambda user: (worker_uid, 1, str(tmp_path))
                        if user == containers.WORKER_USER else (engine_uid, 1, str(tmp_path)))
    checks = []
    monkeypatch.setattr(containers, "_engine_container_runtime_version",
                        lambda: checks.append("engine") or "rootless Podman 5")
    monkeypatch.setattr(containers, "_worker_can_run_podman",
                        lambda: pytest.fail("broker must not attempt to switch to the worker UID"))
    monkeypatch.setattr(containers, "_watch_run", lambda server: None)

    class ReadyServer:
        def __init__(self, *args):
            self.socket_path = args[0]

        def serve_forever(self, **kwargs):
            return None

        def server_close(self):
            return None

    monkeypatch.setattr(containers, "_BrokerServer", ReadyServer)
    assert containers._broker_main() == 0
    assert checks == ["engine"]


def test_broker_rejects_checkout_replacement_and_uses_the_pinned_mount_source(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / "marker").write_text("run checkout")
    outside = tmp_path / "host-root"
    outside.mkdir()
    pin = containers._WorktreePin(worktree)
    try:
        calls = _fake_runtime(monkeypatch, tmp_path)
        code, _, _ = containers._execute(["run", "--volume", f"{worktree}:/workspace", "alpine", "true"],
                                         worktree, worktree, runtime_dir=tmp_path, project_name="aiflow-test",
                                         worktree_pin=pin)
        assert code == 0
        command = calls.read_text()
        assert f"/proc/{os.getpid()}/fd/{pin.fd}:/workspace" in command
        assert f"{worktree}:/workspace" not in command

        moved = tmp_path / "original-checkout"
        worktree.rename(moved)
        worktree.symlink_to(outside, target_is_directory=True)
        assert not pin.is_current()
        with pytest.raises(containers.ContainerPolicyError, match="path was replaced"):
            containers._execute(["build", "."], worktree, worktree, runtime_dir=tmp_path,
                                project_name="aiflow-test", worktree_pin=pin)
    finally:
        pin.close()


def test_broker_start_rejects_checkout_replaced_before_pin(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    original_identity = (worktree.stat().st_dev, worktree.stat().st_ino)
    moved = tmp_path / "original-checkout"
    outside = tmp_path / "host-root"
    outside.mkdir()
    worktree.rename(moved)
    worktree.symlink_to(outside, target_is_directory=True)
    with pytest.raises(containers.ContainerPolicyError, match="changed while the broker started"):
        containers._WorktreePin(worktree, original_identity)


def test_worker_probe_uses_run_groups_and_flags_reachable_engine_sockets(monkeypatch):
    monkeypatch.setattr(containers, "_identity", lambda user: (1001, 1, "/"))
    seen: list[list[str]] = []

    def run(argv, code=127, **kw):
        seen.append(argv)
        reachable = argv[-1] == "/run/docker.sock"
        return subprocess.CompletedProcess(argv, 0 if reachable else (1 if argv[5] == "sh" else code), "", "")

    monkeypatch.setattr(containers.subprocess, "run", run)
    assert containers._worker_can_run_podman() is True
    assert all("--init-groups" in call and "--clear-groups" not in call for call in seen)
    monkeypatch.setattr(containers.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 127 if argv[5] == "podman" else 1, "", ""))
    assert containers._worker_can_run_podman() is False


def test_worker_podman_probe_treats_not_executable_as_denied(monkeypatch):
    monkeypatch.setattr(containers, "_identity", lambda user: (1001, 1, "/"))
    for code, expected in ((126, False), (127, False), (0, True), (1, True)):
        monkeypatch.setattr(containers.subprocess, "run", lambda argv, code=code, **kw:
                            subprocess.CompletedProcess(argv, code, "", ""))
        assert containers._worker_can_run_podman() is expected


def _engine_available() -> bool:
    import shutil
    try:
        return shutil.which("podman") is not None and containers.safe_container_runtime_version() != "missing"
    except Exception:
        return False


@pytest.mark.skipif(os.environ.get("AIFLOW4_TEST_PODMAN") != "1" or not _engine_available(),
                    reason="opt-in: set AIFLOW4_TEST_PODMAN=1 on a node with the safe container runtime")
def test_real_podman_builds_runs_worktree_and_rejects_forbidden_mounts(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / "Containerfile").write_text("FROM alpine:3.20\nCOPY marker /marker\n")
    (worktree / "marker").write_text("for this checkout")
    other = tmp_path / "another-account"
    other.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    def execute(*argv):
        return containers._execute(list(argv), worktree, worktree, runtime_dir=runtime_dir, project_name="aiflow-it")

    code, _, _ = execute("build", "-t", "aiflow-it:latest", ".")
    assert code == 0
    code, stdout, _ = execute("run", "--rm", "-v", f"{worktree}:/workspace:rw", "aiflow-it:latest",
                              "sh", "-c", "cat /workspace/marker; printf 'written by container\\n' > /workspace/result")
    assert code == 0
    assert "for this checkout" in stdout
    assert (worktree / "result").read_text() == "written by container\n"
    for forbidden in ("/", "/etc/" + "aiflow", str(other)):
        with pytest.raises(containers.ContainerPolicyError):
            execute("run", "--rm", "-v", f"{forbidden}:/mnt", "aiflow-it:latest", "true")


def test_runtime_setup_failure_revokes_the_worktree_grant_and_removes_run_directories(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (tmp_path / "home").mkdir()
    spec = SimpleNamespace(cwd=str(worktree), extra_env={}, container_start_hook=None)
    monkeypatch.setattr(containers, "scratch_root", lambda spec: tmp_path / "scratch")
    monkeypatch.setattr(containers, "_identity", lambda user: (os.getuid(), os.getgid(), str(tmp_path / "home")))
    monkeypatch.setattr(containers, "safe_container_runtime_version", lambda: "rootless Podman 5")
    monkeypatch.setattr(containers, "_broker_invocation", lambda root, home: (["broker"], str(tmp_path)))
    monkeypatch.setattr(containers.os, "chown", lambda *a, **k: None)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def broker_fails(*args, **kwargs):
        raise OSError("cannot start broker")

    monkeypatch.setattr(containers.subprocess, "run", fake_run)
    monkeypatch.setattr(containers.subprocess, "Popen", broker_fails)
    runtime = containers.ContainerRuntime(spec, ["setpriv"])
    with pytest.raises(OSError):
        runtime.__enter__()
    assert any(call[:4] == ["setfacl", "-R", "-P", "-m"] for call in calls)
    assert any(call[:4] == ["setfacl", "-R", "-P", "-x"] for call in calls)
    assert not runtime.public_dir.exists() and not runtime.runtime_dir.exists()
    assert spec.container_start_hook is None


def test_acl_cleanup_uses_the_pinned_worktree_after_the_path_is_replaced(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    other = tmp_path / "another-run"
    other.mkdir()
    spec = SimpleNamespace(cwd=str(worktree), extra_env={}, container_start_hook=None)
    monkeypatch.setattr(containers, "scratch_root", lambda spec: tmp_path / "scratch")
    monkeypatch.setattr(containers, "_engine_identity", lambda: (os.getuid(), os.getgid(), str(tmp_path / "home")))
    calls: list[tuple[list[str], str]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), os.readlink(f"/proc/self/fd/{argv[-1].split('/')[4]}")))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(containers.subprocess, "run", fake_run)
    runtime = containers.ContainerRuntime(spec, ["setpriv"])
    info = os.lstat(worktree)
    runtime._grant_worktree(worktree.resolve(), (info.st_dev, info.st_ino))
    # The run swaps its checkout for a symlink to a different tree before exit.
    worktree.rename(tmp_path / "moved")
    worktree.symlink_to(other)
    runtime.__exit__(None, None, None)
    revoke, target = calls[-1]
    assert revoke[:4] == ["setfacl", "-R", "-P", "-x"]
    assert str(worktree) not in revoke and str(other) not in revoke
    assert target == str(tmp_path / "moved")
    assert runtime.acl_fd == -1


@pytest.mark.skipif(os.environ.get("AIFLOW4_TEST_PODMAN") != "1" or not _engine_available(),
                    reason="opt-in: set AIFLOW4_TEST_PODMAN=1 on a node with the safe container runtime")
def test_real_podman_creates_a_named_volume_rootless(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / "compose.yaml").write_text(
        "services:\n  app:\n    image: alpine:3.20\n    command: sh -c 'echo ok > /data/f && cat /data/f'\n"
        "    volumes:\n      - data:/data\nvolumes:\n  data: {}\n")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    code, stdout, _ = containers._execute(["compose", "up", "--abort-on-container-exit"], worktree, worktree,
                                          runtime_dir=runtime_dir, project_name="aiflow-it-volume")
    assert code == 0
    assert "ok" in stdout
