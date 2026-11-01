"""Docker-compatible command allow lists and path normalization."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .container_policy import ContainerPolicyError

_RUN_VALUE_OPTIONS = {"--name", "--workdir", "-w", "--env", "-e", "--publish", "-p", "--expose",
                      "--entrypoint", "--user", "-u", "--label", "--platform", "--network", "--hostname",
                      "--dns", "--pull", "--memory", "--cpus"}
_RUN_FLAGS = {"--rm", "-i", "--interactive", "-t", "--tty", "--init"}
_BUILD_VALUE_OPTIONS = {"-f", "--file", "-t", "--tag", "--target", "--platform", "--build-arg", "--progress",
                        "--network"}
_BUILD_FLAGS = {"--pull", "--no-cache", "--quiet"}


def _run_mount_option(argv: list[str], index: int, arg: str, root: Path, cwd: Path,
                      map_source, normalize_mount: Callable) -> tuple[int, tuple[int, str]]:
    if arg == "--mount" or arg in {"-v", "--volume"}:
        if index + 1 >= len(argv):
            raise ContainerPolicyError("mount option needs a value")
        raw = argv[index + 1]
        normalized_value = "mount=" + raw if arg == "--mount" else raw
        normalized = normalize_mount(normalized_value, root, exact_worktree=True, base=cwd, map_source=map_source)
        value = normalized[6:] if arg == "--mount" else normalized
        return index + 2, (index + 1, value)
    raw = "mount=" + arg.split("=", 1)[1]
    normalized = normalize_mount(raw, root, exact_worktree=True, base=cwd, map_source=map_source)
    return index + 1, (index, "--mount=" + normalized[6:])


def _validate_run_network(value: str) -> None:
    if value == "host":
        raise ContainerPolicyError("host networking is not allowed")
    if value not in {"default", "bridge", "none"}:
        raise ContainerPolicyError("only the run-local default, bridge, or no network is allowed")


def _run_value_option(argv: list[str], index: int, arg: str) -> int:
    if index + 1 >= len(argv):
        raise ContainerPolicyError(f"{arg} needs a value")
    value = argv[index + 1]
    if arg == "--label" and value.partition("=")[0] == "aiflow4.run":
        raise ContainerPolicyError("the aiflow4.run label is reserved for broker cleanup")
    if arg == "--network":
        _validate_run_network(value)
    return index + 2


def _consume_run_option(argv: list[str], index: int, root: Path, cwd: Path, map_source,
                        normalize_mount: Callable) -> tuple[int, tuple[int, str] | None, bool]:
    arg = argv[index]
    if arg in _RUN_FLAGS:
        return index + 1, None, False
    if arg in {"-v", "--volume", "--mount"} or arg.startswith("--mount="):
        next_index, mount = _run_mount_option(argv, index, arg, root, cwd, map_source, normalize_mount)
        return next_index, mount, False
    if arg in _RUN_VALUE_OPTIONS:
        return _run_value_option(argv, index, arg), None, False
    if arg.startswith("--network="):
        _validate_run_network(arg.split("=", 1)[1])
        return index + 1, None, False
    if arg.startswith("-"):
        raise ContainerPolicyError(f"docker run option is not allowed: {arg}")
    return index + 1, None, True


def _parse_run_arguments(argv: list[str], root: Path, cwd: Path, map_source,
                          normalize_mount: Callable) -> tuple[list[tuple[int, str]], int, bool]:
    index, image_seen, mounts, normalized_mounts = 1, False, 0, []
    while index < len(argv):
        if image_seen:
            index += 1
            continue
        index, mount, is_image = _consume_run_option(argv, index, root, cwd, map_source, normalize_mount)
        if mount:
            mounts += 1
            normalized_mounts.append(mount)
        image_seen = image_seen or is_image
    return normalized_mounts, mounts, image_seen


def run_argv(argv: list[str], root: Path, cwd: Path, *, map_source=None, normalize_mount: Callable,
             active_containers: Callable, active_limit: int, layer_limit: str) -> list[str]:
    if not argv or argv[0] != "run":
        raise ContainerPolicyError("only docker build, compose up/run, and mounted docker run are supported")
    normalized_mounts, mount_count, image_seen = _parse_run_arguments(argv, root, cwd, map_source, normalize_mount)
    if not image_seen or mount_count != 1:
        raise ContainerPolicyError("docker run needs exactly one bind mount of this worktree")
    if len(active_containers()) >= active_limit:
        raise ContainerPolicyError("the node container limit is full; retry after a run cleans up")
    normalized = list(argv)
    for index, value in normalized_mounts:
        normalized[index] = value
    return ["run", "--storage-opt", f"size={layer_limit}", "--rm", *normalized[1:]]


def _validate_build_network(value: str) -> None:
    if value == "host":
        raise ContainerPolicyError("host networking during build is not allowed")
    if value not in {"default", "none"}:
        raise ContainerPolicyError("only the default or no build network is allowed")


def _consume_build_option(options: list[str], index: int, root: Path, cwd: Path, map_path,
                          contexts: list[str], inside_file: Callable,
                          inside_directory: Callable) -> int:
    arg = options[index]
    if arg in _BUILD_FLAGS:
        return index + 1
    if arg in _BUILD_VALUE_OPTIONS:
        if index + 1 >= len(options):
            raise ContainerPolicyError(f"{arg} needs a value")
        value = options[index + 1]
        if arg in {"-f", "--file"}:
            validated = inside_file(value, root, base=cwd)
            if map_path:
                options[index + 1] = str(map_path(validated))
        if arg == "--network":
            _validate_build_network(value)
        return index + 2
    if arg.startswith("--network="):
        _validate_build_network(arg.split("=", 1)[1])
        return index + 1
    if arg.startswith("-"):
        raise ContainerPolicyError(f"docker build option is not allowed: {arg}")
    validated = inside_directory(arg, root, base=cwd)
    options[index] = str(map_path(validated)) if map_path else str(validated)
    contexts.append(options[index])
    return index + 1


def build_argv(argv: list[str], root: Path, cwd: Path, *, map_path=None,
               inside_file: Callable, inside_directory: Callable, build_limit: str) -> list[str]:
    if not argv or argv[0] != "build":
        raise ContainerPolicyError("only docker build is supported")
    options, contexts, index = list(argv), [], 1
    while index < len(options):
        index = _consume_build_option(options, index, root, cwd, map_path, contexts, inside_file, inside_directory)
    if len(contexts) != 1:
        raise ContainerPolicyError("docker build needs exactly one local worktree context")
    # The context was replaced above by its validated canonical path (or the pinned fd path).
    return ["build", "--storage-opt", f"size={build_limit}", *options[1:]]
