"""Command preparation, storage checks, and process supervision for the container broker."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .container_policy import ContainerPolicyError, _within

_OUTPUT_LIMIT = 16 * 1024 * 1024


def _prepare_command(argv: list[str], root: Path, run_cwd: Path, runtime_dir: Path, project_name: str,
                     map_worktree_path: Callable | None, read_worktree_file: Callable | None,
                     build_argv: Callable, run_argv: Callable, compose_command: Callable
                     ) -> tuple[str, list[Path], list[str]]:
    if argv and argv[0] == "build":
        podman_argv = build_argv(argv, root, run_cwd, map_path=map_worktree_path)
        compose_files = []
        backend = "podman"
    elif argv and argv[0] == "run":
        podman_argv = run_argv(argv, root, run_cwd, map_source=map_worktree_path)
        compose_files = []
        backend = "podman"
    elif argv and argv[0] == "compose":
        podman_argv, compose_files, _ = compose_command(
            argv, root, run_cwd, runtime_dir, project_name,
            map_worktree_path=map_worktree_path, read_worktree_file=read_worktree_file)
        backend = "podman-compose"
    else:
        raise ContainerPolicyError("only docker build, compose up, and mounted docker run are supported")
    if argv[0] == "run":
        podman_argv[1:1] = ["--label", f"aiflow4.run={project_name}"]
    return backend, compose_files, podman_argv


def _prune_expired_images(image_prune_age: str) -> None:
    subprocess.run(["podman", "image", "prune", "--all", "--filter", f"until={image_prune_age}", "--force"],
                   capture_output=True, timeout=300)


def _check_graphroot_storage(graphroot: Path, cache_limit: int, total_limit: int, disk_reserve: int,
                             image_prune_age: str, directory_size: Callable, disk_usage: Callable) -> None:
    usage = directory_size(graphroot)
    if usage >= cache_limit:
        _prune_expired_images(image_prune_age)
        usage = directory_size(graphroot)
    if usage >= cache_limit:
        raise ContainerPolicyError("container image cache reached its enforced storage limit")
    if disk_usage(graphroot).free < total_limit - usage + 4 * 1024**3:
        _prune_expired_images(image_prune_age)
        usage = directory_size(graphroot)
        free = disk_usage(graphroot).free
        if free < total_limit - usage + 4 * 1024**3:
            raise ContainerPolicyError("container runtime stopped before its storage limit and disk reserve")


def _check_worktree_storage(storage_root: Path, worktree_limit: int, disk_reserve: int,
                            worktree_storage_size: Callable, disk_usage: Callable) -> None:
    usage = worktree_storage_size(storage_root)
    if usage >= worktree_limit:
        raise ContainerPolicyError("container worktree reached its enforced storage limit")
    if disk_usage(storage_root).free < worktree_limit - usage + disk_reserve:
        raise ContainerPolicyError("worktree filesystem cannot preserve its storage limit and disk reserve")


def _append_limited(output: bytearray, chunk: bytes) -> None:
    output.extend(chunk)
    if len(output) > _OUTPUT_LIMIT:
        del output[:len(output) - _OUTPUT_LIMIT]


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _interruption_message(worktree_pin: Any, stop_event: threading.Event | None,
                          graphroot: Path, root: Path, cache_limit: int, disk_reserve: int,
                          directory_size: Callable, disk_usage: Callable,
                          worktree_storage_limit_reached: Callable) -> str | None:
    if worktree_pin and not worktree_pin.is_current():
        return "container worktree path was replaced; command stopped\n"
    if stop_event is not None and stop_event.is_set():
        return "container command stopped because its AI run ended\n"
    if (directory_size(graphroot) >= cache_limit or disk_usage(graphroot).free <= disk_reserve):
        return "aiflow container storage cap reached; command stopped\n"
    if worktree_storage_limit_reached(root):
        return "aiflow container worktree storage cap reached; command stopped\n"
    return None


def _drain_ready_output(selector: selectors.BaseSelector, output: bytearray) -> None:
    for ready, _ in selector.select(timeout=0.2):
        chunk = os.read(ready.fd, 65536)
        if chunk:
            _append_limited(output, chunk)


def _drain_remaining_output(proc: subprocess.Popen, output: bytearray) -> None:
    if not proc.stdout:
        return
    while chunk := os.read(proc.stdout.fileno(), 65536):
        _append_limited(output, chunk)


def _run_process(proc: subprocess.Popen, project_name: str, stop_event: threading.Event | None,
                 worktree_pin: Any, root: Path, graphroot: Path, cache_limit: int, disk_reserve: int,
                 directory_size: Callable, disk_usage: Callable, worktree_storage_limit_reached: Callable,
                 stop_run_containers: Callable) -> tuple[int, str, str]:
    selector = selectors.DefaultSelector()
    if proc.stdout:
        selector.register(proc.stdout, selectors.EVENT_READ)
    output = bytearray()
    try:
        while proc.poll() is None:
            message = _interruption_message(worktree_pin, stop_event, graphroot, root, cache_limit, disk_reserve,
                                            directory_size, disk_usage, worktree_storage_limit_reached)
            if message:
                _terminate_process(proc)
                stop_run_containers(project_name)
                output.extend(message.encode())
                return 125, output.decode(errors="replace"), ""
            _drain_ready_output(selector, output)
        _drain_remaining_output(proc, output)
        if worktree_pin and not worktree_pin.is_current():
            stop_run_containers(project_name)
            output.extend(b"container worktree path was replaced; command stopped\n")
            return 125, output.decode(errors="replace"), ""
        return proc.returncode or 0, output.decode(errors="replace"), ""
    finally:
        selector.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            stop_run_containers(project_name)


def execute_locked(argv: list[str], cwd: Path, worktree: Path, *, runtime_dir: Path,
                   project_name: str, stop_event: threading.Event | None = None,
                   worktree_pin: Any = None, build_argv: Callable, run_argv: Callable,
                   compose_command: Callable, graphroot: Callable, directory_size: Callable,
                   worktree_storage_size: Callable, disk_usage: Callable,
                   worktree_storage_limit_reached: Callable, stop_run_containers: Callable,
                   cache_limit: int, total_limit: int, worktree_limit: int, disk_reserve: int,
                   image_prune_age: str) -> tuple[int, str, str]:
    if worktree_pin:
        worktree_pin.assert_current()
    root = worktree_pin.path if worktree_pin else worktree.resolve(strict=True)
    run_cwd = cwd.resolve(strict=True)
    if not _within(run_cwd, root):
        raise ContainerPolicyError("container working directory must stay inside the worktree")
    map_path = worktree_pin.map_path if worktree_pin else None
    read_file = worktree_pin.read_text if worktree_pin else None
    engine_cwd = map_path(run_cwd) if map_path else run_cwd
    backend, _, podman_argv = _prepare_command(argv, root, run_cwd, runtime_dir, project_name,
                                                map_path, read_file, build_argv, run_argv, compose_command)
    graph = graphroot()
    _check_graphroot_storage(graph, cache_limit, total_limit, disk_reserve, image_prune_age,
                             directory_size, disk_usage)
    storage_root = worktree_pin.proc_path if worktree_pin else root
    _check_worktree_storage(storage_root, worktree_limit, disk_reserve, worktree_storage_size, disk_usage)
    if worktree_pin:
        worktree_pin.assert_current()
    command = [backend, *podman_argv] if backend == "podman-compose" else ["podman", *podman_argv]
    proc = subprocess.Popen(command, cwd=str(engine_cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    return _run_process(proc, project_name, stop_event, worktree_pin, root, graph, cache_limit, disk_reserve,
                        directory_size, disk_usage, worktree_storage_limit_reached, stop_run_containers)
