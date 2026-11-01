"""Golden command and fake-process tests for the safe container broker."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.worker import containers


@pytest.mark.parametrize(
    ("builder", "argv", "expected"),
    [
        pytest.param("build", ["build", "."],
                     ["build", "--storage-opt", "size=2g", "{root}"], id="build-default"),
        pytest.param("build", ["build", "-t", "web:dev", "."],
                     ["build", "--storage-opt", "size=2g", "-t", "web:dev", "{root}"], id="build-tag"),
        pytest.param("build", ["build", "-f", "Containerfile", "."],
                     ["build", "--storage-opt", "size=2g", "-f", "Containerfile", "{root}"],
                     id="build-file"),
        pytest.param("build", ["build", "--build-arg", "MODE=release", "--network=none", "context"],
                     ["build", "--storage-opt", "size=2g", "--build-arg", "MODE=release", "--network=none",
                      "{root}/context"], id="build-args-and-network"),
        pytest.param("build", ["build", "--platform", "linux/amd64", "--no-cache", "--quiet", "."],
                     ["build", "--storage-opt", "size=2g", "--platform", "linux/amd64", "--no-cache", "--quiet",
                      "{root}"], id="build-platform-flags"),
        pytest.param("run", ["run", "-v", "{root}:/workspace", "alpine", "true"],
                     ["run", "--storage-opt", "size=1g", "--rm", "-v", "{root}:/workspace", "alpine", "true"],
                     id="run-volume"),
        pytest.param("run", ["run", "--mount", "type=bind,source={root},target=/workspace", "alpine", "pwd"],
                     ["run", "--storage-opt", "size=1g", "--rm", "--mount",
                      "type=bind,source={root},target=/workspace", "alpine", "pwd"], id="run-mount"),
        pytest.param("run", ["run", "-e", "MODE=dev", "--publish", "8080:80", "--volume", "{root}:/app",
                              "python:3", "app.py"],
                     ["run", "--storage-opt", "size=1g", "--rm", "-e", "MODE=dev", "--publish", "8080:80",
                      "--volume", "{root}:/app", "python:3", "app.py"], id="run-env-publish"),
        pytest.param("run", ["run", "--network=none", "-v", "{root}:/work:ro", "alpine", "cat", "/work/x"],
                     ["run", "--storage-opt", "size=1g", "--rm", "--network=none", "-v", "{root}:/work:ro",
                      "alpine", "cat", "/work/x"], id="run-network-read-only"),
        pytest.param("run", ["run", "-i", "-t", "--workdir", "/workspace", "-v", "{root}:/workspace",
                              "alpine", "sh"],
                     ["run", "--storage-opt", "size=1g", "--rm", "-i", "-t", "--workdir", "/workspace", "-v",
                      "{root}:/workspace", "alpine", "sh"], id="run-interactive"),
        pytest.param("compose", ["compose", "up"],
                     ["-f", "{runtime}/compose-input-000001.yaml", "--env-file", "{runtime}/compose-env-000002.env",
                      "-f", "{runtime}/compose-000003.yaml", "-p", "aiflow-run", "up"], id="compose-default-up"),
        pytest.param("compose", ["compose", "-f", "compose.yaml", "up", "-d"],
                     ["-f", "{runtime}/compose-input-000001.yaml", "--env-file", "{runtime}/compose-env-000002.env",
                      "-f", "{runtime}/compose-000003.yaml", "-p", "aiflow-run", "up", "-d"],
                     id="compose-explicit-file-detached"),
        pytest.param("compose", ["compose", "--project-directory", ".", "--profile", "dev", "up", "web"],
                     ["--profile", "dev", "-f", "{runtime}/compose-input-000001.yaml", "--env-file",
                      "{runtime}/compose-env-000002.env", "-f", "{runtime}/compose-000003.yaml", "-p", "aiflow-run",
                      "up", "web"], id="compose-profile-service"),
        pytest.param("compose", ["compose", "run", "web", "echo", "hello"],
                     ["-f", "{runtime}/compose-input-000001.yaml", "--env-file", "{runtime}/compose-env-000002.env",
                      "-f", "{runtime}/compose-000003.yaml", "-p", "aiflow-run", "run", "--rm", "web", "echo",
                      "hello"], id="compose-run-adds-rm"),
        pytest.param("compose", ["compose", "--env-file", ".env.dev", "up", "--abort-on-container-exit"],
                     ["-f", "{runtime}/compose-input-000001.yaml", "--env-file", "{runtime}/compose-env-000002.env",
                      "-f", "{runtime}/compose-000003.yaml", "-p", "aiflow-run", "up", "--abort-on-container-exit"],
                     id="compose-explicit-env-file"),
    ],
)
def test_command_builders_match_current_golden_argv(monkeypatch, tmp_path, builder, argv, expected):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "Containerfile").write_text("FROM scratch\n")
    (root / "context").mkdir()
    (root / "data").mkdir()
    (root / "compose.yaml").write_text("services:\n  web:\n    image: alpine\n")
    (root / ".env.dev").write_text("MODE=dev\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(containers, "_active_containers", lambda: [])
    monkeypatch.setattr(containers, "_engine_identity", lambda: (os.getuid(), os.getgid(), str(tmp_path)))
    names = iter(("000001", "000002", "000003"))
    monkeypatch.setattr(containers.secrets, "token_hex", lambda _: next(names))
    actual_argv = [item.format(root=root, runtime=runtime) for item in argv]
    if builder == "build":
        actual = containers._build_argv(actual_argv, root, root)
    elif builder == "run":
        actual = containers._run_argv(actual_argv, root, root)
    else:
        actual, _, _ = containers._compose_command(actual_argv, root, root, runtime, "aiflow-run")
    expected_argv = [item.format(root=root, runtime=runtime) for item in expected]
    assert actual == expected_argv


def test_execute_builds_the_same_process_request_with_a_fake_runner(monkeypatch, tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    calls = []

    class FinishedProcess:
        pid = 12345
        stdout = None
        returncode = 7

        def poll(self):
            return self.returncode

    monkeypatch.setattr(containers, "_graphroot", lambda: tmp_path)
    monkeypatch.setattr(containers, "_directory_size", lambda _: 0)
    monkeypatch.setattr(containers, "_worktree_storage_size", lambda _: 0)
    monkeypatch.setattr(containers.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(containers.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    monkeypatch.setattr(containers.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
                        or FinishedProcess())
    result = containers._execute(["build", "-t", "golden", "."], worktree, worktree,
                                 runtime_dir=tmp_path, project_name="aiflow-run")
    assert result == (7, "", "")
    assert calls == [((["podman", "build", "--storage-opt", "size=2g", "-t", "golden", str(worktree)],), {
        "cwd": str(worktree), "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "start_new_session": True,
    })]


def test_broker_cleanup_removes_project_resources_in_order_with_a_fake_runner(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        output = ""
        if argv[1:3] == ["ps", "-aq"]:
            output = "container-a\n"
        elif argv[1:3] == ["volume", "ls"]:
            output = "volume-a\n"
        elif argv[1:3] == ["network", "ls"]:
            output = "network-a\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(containers.subprocess, "run", fake_run)
    server = object.__new__(containers._BrokerServer)
    server._cleanup_lock = threading.Lock()
    server._cleaned = False
    server.compose_used = True
    server.project_name = "aiflow-run"
    server.cleanup()
    server.cleanup()
    assert calls == [
        ["podman", "ps", "-aq", "--filter", "label=aiflow4.run=aiflow-run"],
        ["podman", "stop", "--time", "1", "container-a"],
        ["podman", "rm", "--force", "--volumes", "container-a"],
        ["podman", "volume", "ls", "-q", "--filter", "label=io.podman.compose.project=aiflow-run"],
        ["podman", "volume", "rm", "volume-a"],
        ["podman", "network", "ls", "-q", "--filter", "label=io.podman.compose.project=aiflow-run"],
        ["podman", "network", "rm", "network-a"],
    ]
