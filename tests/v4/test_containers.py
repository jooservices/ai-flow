"""Safe container broker command, path, cache and storage policy tests."""
from __future__ import annotations

import os
import json
import subprocess
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
    assert commands[0].startswith("build --storage-opt size=2g -t sample .")
    assert commands[1].startswith("run --storage-opt size=1g --rm --volume ")


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
    override = Path(up[up.index("-f", 2) + 1])
    assert "size: 1g" in override.read_text()
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
              "    extends:\n      file: ../other-compose.yaml\n      service: base\n"]
    for service in unsafe:
        compose.write_text("services:\n  test:\n    image: alpine\n" + service)
        data = __import__("yaml").safe_load(compose.read_text())
        with pytest.raises(containers.ContainerPolicyError):
            containers._validate_compose_document(data, compose, worktree, worktree)


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
    (worktree / "Containerfile").write_text("FROM scratch\nCOPY marker /marker\n")
    (worktree / "marker").write_text("for this checkout")
    other = tmp_path / "another-account"
    other.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    def execute(*argv):
        return containers._execute(list(argv), worktree, worktree, runtime_dir=runtime_dir, project_name="aiflow-it")

    code, _, _ = execute("build", "-t", "aiflow-it:latest", ".")
    assert code == 0
    execute("run", "--rm", "-v", f"{worktree}:/workspace:ro", "aiflow-it:latest", "/marker")  # policy accepts it
    for forbidden in ("/", "/etc/" + "aiflow", str(other)):
        with pytest.raises(containers.ContainerPolicyError):
            execute("run", "--rm", "-v", f"{forbidden}:/mnt", "aiflow-it:latest", "true")
