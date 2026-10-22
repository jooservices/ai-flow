"""Rootless Podman broker for the deliberately small Docker-compatible interface AI runs need.

The AI process never receives Podman's storage or a container-engine socket. Its `docker` shim sends argv to
this per-run broker, which validates the worktree, Compose inputs, mounts, namespaces and storage limits before
executing Podman. The boundary is enforced by the operating system: Podman and its storage belong to a separate
unprivileged account (`aiflow-containers`), the AI run and project checks stay `aiflow-worker`, and the preflight
reports the runtime missing unless that account's storage is private and `aiflow-worker` cannot execute Podman at
all, so calling the real binary by absolute path fails. Rootless storage is shared on the node for image caching.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import selectors
import secrets
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import yaml

from ..adapters.base import RunSpec, scratch_root

WORKER_USER = "aiflow-worker"
ENGINE_USER = "aiflow-containers"
RUNTIME_TOOL = "safe-container-runtime"
IMAGE_CACHE_LIMIT = 12 * 1024**3
TOTAL_STORAGE_LIMIT = 20 * 1024**3
WORKTREE_STORAGE_LIMIT = 4 * 1024**3
STORAGE_DISK_RESERVE = 4 * 1024**3
ACTIVE_CONTAINER_LIMIT = 8
CONTAINER_LAYER_LIMIT = "1g"
BUILD_LIMIT = "2g"
IMAGE_PRUNE_AGE = "168h"
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")


class ContainerPolicyError(ValueError):
    """A command or compose file requested access outside the run's container policy."""


def _identity(user: str) -> tuple[int, int, str]:
    entry = pwd.getpwnam(user)
    return entry.pw_uid, entry.pw_gid, entry.pw_dir


def _engine_identity() -> tuple[int, int, str]:
    """The account that owns Podman and its storage; never the account AI runs execute as."""
    return _identity(ENGINE_USER)


def engine_prefix() -> list[str]:
    uid, gid, _ = _engine_identity()
    return ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--"]


def _worker_can_run_podman() -> bool:
    """True when the AI run's own account could execute the real Podman (so it could bypass the broker)."""
    uid, gid, _ = _identity(WORKER_USER)
    try:
        probe = subprocess.run(["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--",
                                "podman", "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return True
    # 126/127: found but not executable, or not found for that account. Anything else is not proof of denial.
    return probe.returncode not in {126, 127}


def _private_to_engine(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return info.st_uid == _engine_identity()[0] and not info.st_mode & 0o077


def _run_as_engine(argv: list[str], *, timeout: int = 20, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    _, _, home = _engine_identity()
    base = [*engine_prefix(), *argv]
    worker_env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home,
                  "USER": ENGINE_USER, "LOGNAME": ENGINE_USER, "LANG": "C.UTF-8",
                  "TMPDIR": str(Path(home) / ".cache" / "aiflow4" / "tmp")}
    if env:
        worker_env.update(env)
    return subprocess.run(base, capture_output=True, text=True, timeout=timeout, env=worker_env)


def safe_container_runtime_version() -> str:
    """Report healthy only for rootless, quota-backed Podman whose engine account AI runs cannot reach or execute."""
    try:
        if _engine_identity()[0] == _identity(WORKER_USER)[0] or _worker_can_run_podman():
            return "missing"
        info = _run_as_engine(["podman", "info", "--format", "json"], timeout=20)
        if info.returncode:
            return "missing"
        data = json.loads(info.stdout)
        host = data.get("host") or {}
        if (host.get("security") or {}).get("rootless") is not True:
            return "missing"
        namespaces = _run_as_engine(["podman", "unshare", "true"], timeout=20)
        if namespaces.returncode:
            return "missing"
        store = data.get("store") or {}
        graphroot = str(store.get("graphRoot") or "")
        if not graphroot or not Path(graphroot).is_dir():
            return "missing"
        if not _private_to_engine(Path(graphroot)) or not _private_to_engine(Path(_engine_identity()[2])):
            return "missing"
        quota = subprocess.run(["findmnt", "-T", graphroot, "-n", "-o", "FSTYPE,OPTIONS"], capture_output=True,
                               text=True, timeout=10)
        fields = (quota.stdout or "").strip().split(None, 1) if quota.returncode == 0 else []
        filesystem, options_text = fields if len(fields) == 2 else ("", "")
        options = set(options_text.split(","))
        if filesystem not in {"xfs", "ext4"} or not options.intersection({"pquota", "prjquota"}):
            return "missing"
        provider = _run_as_engine(["podman-compose", "--version"], timeout=15)
        if provider.returncode:
            return "missing"
        podman_version = str((data.get("version") or {}).get("Version") or "")
        compose_version = (provider.stdout or provider.stderr).strip().splitlines()
        if not podman_version or not compose_version:
            return "missing"
        usage = shutil.disk_usage(graphroot)
        if usage.total < TOTAL_STORAGE_LIMIT + 4 * 1024**3:
            return "missing"
        return f"rootless Podman {podman_version}; {compose_version[0][:60]}; project quota"
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return "missing"


def prune_image_cache() -> bool:
    """Remove abandoned containers and unused images older than seven days from the node cache."""
    if safe_container_runtime_version() == "missing":
        return False
    try:
        containers = _run_as_engine(["podman", "container", "prune", "--filter", "until=24h", "--force"],
                                     timeout=300)
        images = _run_as_engine(["podman", "image", "prune", "--all", "--filter", f"until={IMAGE_PRUNE_AGE}",
                                 "--force"], timeout=300)
        return containers.returncode == 0 and images.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _within(path: str | Path, root: Path) -> bool:
    try:
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = root / resolved
        resolved = resolved.resolve(strict=False)
        base = root.resolve(strict=True)
        return resolved == base or base in resolved.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _inside_file(path: str, root: Path, *, base: Path | None = None) -> Path:
    if not path or any(char in path for char in "$`\\") or path.startswith("~"):
        raise ContainerPolicyError("file paths must be literal paths inside the worktree")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContainerPolicyError("compose and environment files must exist inside the worktree") from exc
    if not _within(candidate, root) or not candidate.is_file():
        raise ContainerPolicyError("compose and environment files must stay inside the worktree")
    return candidate


def _inside_directory(path: str, root: Path, *, base: Path | None = None) -> Path:
    if not path or any(char in path for char in "$`\\") or path.startswith("~"):
        raise ContainerPolicyError("build contexts must be literal directories inside the worktree")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContainerPolicyError("build contexts must exist inside the worktree") from exc
    if not _within(candidate, root) or not candidate.is_dir():
        raise ContainerPolicyError("build contexts must stay inside the worktree")
    return candidate


def _validate_bind(source: str, root: Path, *, exact_worktree: bool = False, base: Path | None = None) -> Path:
    if not source or any(char in source for char in "$`\\") or source.startswith("~"):
        raise ContainerPolicyError("bind mounts must use literal worktree paths")
    candidate = Path(source)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContainerPolicyError("bind mounts must exist inside the worktree") from exc
    if not _within(candidate, root) or (exact_worktree and candidate != root.resolve(strict=True)):
        raise ContainerPolicyError("containers may bind only the current worktree")
    return candidate


def _parse_mount(value: str, root: Path, *, exact_worktree: bool = False, base: Path | None = None) -> None:
    if value.startswith("mount="):
        fields: dict[str, str] = {}
        for field in value[6:].split(","):
            key, sep, item = field.partition("=")
            if not sep or key in fields:
                raise ContainerPolicyError("invalid --mount syntax")
            fields[key] = item
        if fields.get("type") != "bind" or set(fields) - {"type", "src", "source", "dst", "destination", "target", "readonly", "readwrite"}:
            raise ContainerPolicyError("--mount accepts only one worktree bind mount")
        source = fields.get("src", fields.get("source", ""))
        if "readonly" in fields and fields["readonly"].lower() not in {"true", "1"}:
            raise ContainerPolicyError("invalid read-only mount option")
        if "readwrite" in fields and fields["readwrite"].lower() not in {"true", "1"}:
            raise ContainerPolicyError("invalid read-write mount option")
        _validate_bind(source, root, exact_worktree=exact_worktree, base=base)
        return
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise ContainerPolicyError("volume syntax must be source:target[:ro|rw]")
    if len(parts) == 3 and parts[2] not in {"ro", "rw"}:
        raise ContainerPolicyError("bind mount propagation and relabel options are not allowed")
    _validate_bind(parts[0], root, exact_worktree=exact_worktree, base=base)


def _run_argv(argv: list[str], root: Path, cwd: Path) -> list[str]:
    if not argv or argv[0] != "run":
        raise ContainerPolicyError("only docker build, compose up/run, and mounted docker run are supported")
    i, mounts, image_seen = 1, [], False
    value_options = {"--name", "--workdir", "-w", "--env", "-e", "--publish", "-p", "--expose",
                     "--entrypoint", "--user", "-u", "--label", "--platform", "--network", "--hostname",
                     "--dns", "--pull", "--memory", "--cpus"}
    flags = {"--rm", "-i", "--interactive", "-t", "--tty", "--init"}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if not image_seen:
            if arg in flags:
                i += 1
            elif arg in {"-v", "--volume", "--mount"}:
                if i + 1 >= len(argv):
                    raise ContainerPolicyError("mount option needs a value")
                mounts.append(argv[i + 1])
                i += 2
            elif arg.startswith("--mount="):
                mounts.append("mount=" + arg.split("=", 1)[1])
                i += 1
            elif arg in value_options:
                if i + 1 >= len(argv):
                    raise ContainerPolicyError(f"{arg} needs a value")
                if arg == "--label" and argv[i + 1].partition("=")[0] == "aiflow4.run":
                    raise ContainerPolicyError("the aiflow4.run label is reserved for broker cleanup")
                if arg == "--network" and argv[i + 1] == "host":
                    raise ContainerPolicyError("host networking is not allowed")
                if arg == "--network" and argv[i + 1] not in {"default", "bridge", "none"}:
                    raise ContainerPolicyError("only the run-local default, bridge, or no network is allowed")
                i += 2
            elif arg.startswith("--network="):
                if arg.split("=", 1)[1] not in {"default", "bridge", "none"}:
                    raise ContainerPolicyError("only the run-local default, bridge, or no network is allowed")
                i += 1
            elif arg.startswith("-"):
                raise ContainerPolicyError(f"docker run option is not allowed: {arg}")
            else:
                image_seen = True
                i += 1
        else:
            i += 1
    if not image_seen or len(mounts) != 1:
        raise ContainerPolicyError("docker run needs exactly one bind mount of this worktree")
    _parse_mount(mounts[0], root, exact_worktree=True, base=cwd)
    if len(_active_containers()) >= ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError("the node container limit is full; retry after a run cleans up")
    # Do not allow detached containers: the broker must watch their disk use until they exit.
    return ["run", "--storage-opt", f"size={CONTAINER_LAYER_LIMIT}", "--rm", *argv[1:]]


def _build_argv(argv: list[str], root: Path, cwd: Path) -> list[str]:
    if not argv or argv[0] != "build":
        raise ContainerPolicyError("only docker build is supported")
    i, contexts = 1, []
    value_options = {"-f", "--file", "-t", "--tag", "--target", "--platform", "--build-arg", "--progress",
                     "--network"}
    flags = {"--pull", "--no-cache", "--quiet"}
    options = list(argv)
    while i < len(options):
        arg = options[i]
        if arg in flags:
            i += 1
        elif arg in value_options:
            if i + 1 >= len(options):
                raise ContainerPolicyError(f"{arg} needs a value")
            value = options[i + 1]
            if arg in {"-f", "--file"}:
                _inside_file(value, root, base=cwd)
            if arg == "--network" and value == "host":
                raise ContainerPolicyError("host networking during build is not allowed")
            if arg == "--network" and value not in {"default", "none"}:
                raise ContainerPolicyError("only the default or no build network is allowed")
            i += 2
        elif arg.startswith("--network="):
            if arg.split("=", 1)[1] not in {"default", "none"}:
                raise ContainerPolicyError("only the default or no build network is allowed")
            i += 1
        elif arg.startswith("-"):
            raise ContainerPolicyError(f"docker build option is not allowed: {arg}")
        else:
            contexts.append(arg)
            i += 1
    if len(contexts) != 1:
        raise ContainerPolicyError("docker build needs exactly one local worktree context")
    _inside_directory(contexts[0], root, base=cwd)
    return ["build", "--storage-opt", f"size={BUILD_LIMIT}", *options[1:]]


def _short_compose_volume(value: str, project_dir: Path, root: Path, named_volumes: set[str]) -> None:
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise ContainerPolicyError("invalid compose volume syntax")
    source = parts[0]
    if source.startswith(("/", "./", "../")):
        _validate_bind(str(Path(source) if Path(source).is_absolute() else project_dir / source), root)
    elif source.startswith(("~", "$")) or "\\" in source:
        raise ContainerPolicyError("compose bind mounts must use literal worktree paths")
    elif source and source not in named_volumes:
        raise ContainerPolicyError("compose named volumes must be declared in the project")
    if len(parts) == 3 and set(parts[2].split(",")) - {"ro", "rw"}:
        raise ContainerPolicyError("compose mount propagation and relabel options are not allowed")


def _validate_compose_document(data: Any, compose_file: Path, project_dir: Path, root: Path,
                               *, require_services: bool = True) -> int:
    if not isinstance(data, dict):
        raise ContainerPolicyError("compose file must contain a mapping")
    if "include" in data:
        raise ContainerPolicyError("compose include is disabled; all compose inputs must be explicit worktree files")
    raw_volumes = data.get("volumes") or {}
    if not isinstance(raw_volumes, dict):
        raise ContainerPolicyError("compose volumes must be a mapping")
    named_volumes = set(raw_volumes)
    for name, volume in raw_volumes.items():
        if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(volume or {}, dict):
            raise ContainerPolicyError("invalid compose volume declaration")
        if (volume or {}).get("name"):
            raise ContainerPolicyError("custom compose volume names are not allowed; volumes are named per run")
        if ((volume or {}).get("external") or (volume or {}).get("driver_opts")
                or (volume or {}).get("driver") not in (None, "", "local")):
            raise ContainerPolicyError("external and host-backed named volumes are not allowed")
    for section in ("secrets", "configs"):
        entries = data.get(section) or {}
        if not isinstance(entries, dict):
            raise ContainerPolicyError(f"compose {section} must be a mapping")
        for name, entry in entries.items():
            if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(entry or {}, dict):
                raise ContainerPolicyError(f"invalid compose {section} declaration")
            entry = entry or {}
            if entry.get("external"):
                raise ContainerPolicyError(f"external compose {section} are not allowed")
            if entry.get("file"):
                _inside_file(str(entry["file"]), root, base=project_dir)
    networks = data.get("networks") or {}
    if not isinstance(networks, dict):
        raise ContainerPolicyError("compose networks must be a mapping")
    for name, network in networks.items():
        network = network or {}
        if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(network, dict):
            raise ContainerPolicyError("invalid compose network declaration")
        if (network.get("external") or network.get("name") or network.get("driver_opts")
                or network.get("driver") not in (None, "", "bridge")):
            raise ContainerPolicyError("external, named, host-backed, and custom compose networks are not allowed")
    services = data.get("services") or {}
    if not isinstance(services, dict) or (require_services and not services) or len(services) > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError(f"compose must define between 1 and {ACTIVE_CONTAINER_LIMIT} services")
    for name, service in services.items():
        if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(service, dict):
            raise ContainerPolicyError("invalid compose service")
        if (service.get("privileged") not in (None, False) or service.get("cap_add") or service.get("devices")
                or service.get("device_cgroup_rules")):
            raise ContainerPolicyError("privileged containers, added capabilities, and host devices are not allowed")
        labels = service.get("labels") or {}
        label_keys = labels.keys() if isinstance(labels, dict) else labels if isinstance(labels, list) else []
        if any(str(label).partition("=")[0] == "aiflow4.run" for label in label_keys):
            raise ContainerPolicyError("the aiflow4.run label is reserved for broker cleanup")
        if service.get("volumes_from") or service.get("security_opt") or service.get("runtime"):
            raise ContainerPolicyError("host container controls and inherited mounts are not allowed")
        if service.get("extends") or service.get("develop"):
            raise ContainerPolicyError("compose service inheritance and host-side watch settings are not allowed")
        for key in ("pid", "ipc", "uts", "userns_mode", "cgroup"):
            if service.get(key) not in (None, ""):
                raise ContainerPolicyError(f"custom {key} namespaces are not allowed")
        network_mode = str(service.get("network_mode") or "").lower()
        if network_mode:
            raise ContainerPolicyError("custom container network modes are not allowed")
        service_networks = service.get("networks") or []
        if isinstance(service_networks, dict):
            service_networks = list(service_networks)
        if not isinstance(service_networks, list):
            raise ContainerPolicyError("compose service networks must be a list or mapping")
        if any(str(item) not in networks and str(item) != "default" for item in service_networks):
            raise ContainerPolicyError("compose services may use only networks declared in this project")
        if service.get("build"):
            build = service["build"]
            build = {"context": build} if isinstance(build, str) else build
            if (not isinstance(build, dict) or build.get("ssh") or build.get("secrets") or build.get("additional_contexts")
                    or build.get("privileged") not in (None, False)):
                raise ContainerPolicyError("build secrets, ssh agents, and additional contexts are not allowed")
            context = _inside_directory(str(build.get("context") or "."), root, base=project_dir)
            dockerfile = build.get("dockerfile")
            if dockerfile:
                _inside_file(str(context / str(dockerfile)), root)
            if str(build.get("network") or "").lower() not in {"", "default", "none"}:
                raise ContainerPolicyError("host networking during build is not allowed")
        env_files = service.get("env_file") or []
        if isinstance(env_files, (str, dict)):
            env_files = [env_files]
        for item in env_files:
            path = item.get("path") if isinstance(item, dict) else item
            _inside_file(str(path), root, base=project_dir)
        for volume in service.get("volumes") or []:
            if isinstance(volume, str):
                _short_compose_volume(volume, project_dir, root, named_volumes)
            elif isinstance(volume, dict):
                kind = str(volume.get("type") or "volume")
                if kind == "bind":
                    source = str(volume.get("source") or "")
                    _validate_bind(source if Path(source).is_absolute() else str(project_dir / source), root)
                    bind = volume.get("bind") or {}
                    if bind.get("propagation"):
                        raise ContainerPolicyError("bind propagation is not allowed")
                elif kind == "volume":
                    source = str(volume.get("source") or "")
                    if source and source not in named_volumes:
                        raise ContainerPolicyError("compose named volumes must be declared in the project")
                elif kind != "tmpfs":
                    raise ContainerPolicyError(f"compose mount type is not allowed: {kind}")
            else:
                raise ContainerPolicyError("invalid compose volume")
    return len(services)


def _compose_command(argv: list[str], root: Path, cwd: Path, runtime_dir: Path,
                     project_name: str) -> tuple[list[str], list[Path], str]:
    if not argv or argv[0] != "compose":
        raise ContainerPolicyError("only docker compose up and run are supported")
    i, files, env_files, project_dir, project_args = 1, [], [], None, []
    while i < len(argv):
        arg = argv[i]
        if arg in {"-f", "--file", "--env-file", "--project-directory"}:
            if i + 1 >= len(argv):
                raise ContainerPolicyError(f"{arg} needs a value")
            value = argv[i + 1]
            if arg in {"-f", "--file"}:
                files.append(_inside_file(value, root, base=cwd))
                project_args.extend(["-f", str(files[-1])])
            elif arg == "--env-file":
                env_files.append(_inside_file(value, root, base=cwd))
                project_args.extend(["--env-file", str(env_files[-1])])
            else:
                project_dir = _inside_directory(value, root, base=cwd)
                project_args.extend(["--project-directory", str(project_dir)])
            i += 2
        elif arg.startswith("--file=") or arg.startswith("--env-file=") or arg.startswith("--project-directory="):
            name, value = arg.split("=", 1)
            if name == "--file":
                files.append(_inside_file(value, root, base=cwd))
                project_args.extend(["-f", str(files[-1])])
            elif name == "--env-file":
                env_files.append(_inside_file(value, root, base=cwd))
                project_args.extend(["--env-file", str(env_files[-1])])
            else:
                project_dir = _inside_directory(value, root, base=cwd)
                project_args.extend(["--project-directory", str(project_dir)])
            i += 1
        elif arg in {"-p", "--project-name"}:
            raise ContainerPolicyError("compose project names are assigned per run")
        elif arg.startswith("--project-name="):
            raise ContainerPolicyError("compose project names are assigned per run")
        elif arg in {"--profile", "--ansi", "--progress"}:
            if i + 1 >= len(argv):
                raise ContainerPolicyError(f"{arg} needs a value")
            if arg == "--profile":
                project_args.extend([arg, argv[i + 1]])
            i += 2
        elif arg.startswith("--profile=") or arg.startswith("--ansi=") or arg.startswith("--progress="):
            if arg.startswith("--profile="):
                project_args.append(arg)
            i += 1
        else:
            break
    if not files:
        default = next((cwd / name for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
                        if (cwd / name).is_file()), None)
        if default is None:
            raise ContainerPolicyError("no compose file was found in the worktree")
        files.append(_inside_file(str(default), root))
        project_args.extend(["-f", str(files[-1])])
    project_dir = project_dir or files[0].parent
    if not _within(project_dir, root):
        raise ContainerPolicyError("compose project directory must be inside the worktree")
    service_count = 0
    all_services: dict[str, dict[str, Any]] = {}
    for index, compose_file in enumerate(files):
        try:
            data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ContainerPolicyError("compose file could not be read") from exc
        count = _validate_compose_document(data, compose_file, project_dir, root, require_services=index == 0)
        service_count = max(service_count, count)
        all_services.update((data.get("services") or {}) if isinstance(data, dict) else {})
    service_count = len(all_services)
    if not service_count:
        raise ContainerPolicyError("compose file does not define services")
    if service_count > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError(f"compose may define at most {ACTIVE_CONTAINER_LIMIT} services")
    if len(_active_containers()) + service_count > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError("the node container limit is full; retry after a run cleans up")
    if i >= len(argv) or argv[i] not in {"up", "run"}:
        raise ContainerPolicyError("only docker compose up and run are supported")
    subcommand, tail = argv[i], argv[i + 1:]
    if subcommand == "run":
        # Compose run may start dependency services; reserve the entire file's service count and remove its runner.
        if not tail:
            raise ContainerPolicyError("docker compose run needs a service")
        safe_flags = {"--rm", "--build", "--no-deps", "--quiet-pull", "-T", "--no-TTY",
                      "--service-ports", "--use-aliases"}
        value_flags = {"--entrypoint", "--env", "-e", "--user", "-u", "--workdir", "-w"}
        option_index = 0
        while option_index < len(tail) and tail[option_index].startswith("-"):
            option = tail[option_index]
            if option in safe_flags:
                option_index += 1
            elif option in value_flags:
                if option_index + 1 >= len(tail):
                    raise ContainerPolicyError(f"{option} needs a value")
                option_index += 2
            elif any(option.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
                option_index += 1
            else:
                raise ContainerPolicyError(f"compose run option is not allowed: {option}")
        if option_index == len(tail):
            raise ContainerPolicyError("docker compose run needs a service")
        if not any(item == "--rm" for item in tail):
            tail = ["--rm", *tail]
        service_index = 0
        while service_index < len(tail) and tail[service_index].startswith("-"):
            if tail[service_index] in {"--entrypoint", "--env", "-e", "--user", "-u", "--workdir", "-w"}:
                service_index += 2
            else:
                service_index += 1
        if service_index >= len(tail) or tail[service_index] not in all_services:
            raise ContainerPolicyError("docker compose run must name a service in this compose project")
    else:
        if len(tail) > 0 and tail[0].startswith("-"):
            safe = {"-d", "--detach", "--build", "--no-build", "--pull", "--remove-orphans", "--force-recreate",
                    "--no-recreate", "--no-deps", "--wait", "--quiet-pull", "--renew-anon-volumes", "--timestamps"}
            i_tail = 0
            while i_tail < len(tail):
                item = tail[i_tail]
                if item in safe:
                    i_tail += 1
                elif item in {"--timeout", "-t"}:
                    i_tail += 2
                elif item.startswith("--timeout="):
                    i_tail += 1
                elif item.startswith("-"):
                    raise ContainerPolicyError(f"compose up option is not allowed: {item}")
                else:
                    if item not in all_services:
                        raise ContainerPolicyError("compose up may name only configured services")
                    i_tail += 1
    override = runtime_dir / f"compose-{secrets.token_hex(6)}.yaml"
    service_names = list(all_services)
    services_override: dict[str, dict[str, Any]] = {}
    for name in service_names:
        services_override[name] = {"storage_opt": {"size": CONTAINER_LAYER_LIMIT}}
        services_override[name]["labels"] = {"aiflow4.run": project_name}
        service = all_services.get(name) or {}
        if service.get("build"):
            services_override[name]["build"] = {"storage_opt": {"size": BUILD_LIMIT}}
    override.write_text(yaml.safe_dump({"services": services_override}, sort_keys=False), encoding="utf-8")
    uid, gid, _ = _engine_identity()
    os.chown(override, uid, gid)
    override.chmod(0o600)
    project_args.extend(["-f", str(override), "-p", project_name])
    return [*project_args, subcommand, *tail], files, subcommand


def _active_containers() -> list[str]:
    try:
        result = subprocess.run(["podman", "ps", "--all", "--quiet"], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ContainerPolicyError("could not check the rootless container limit")
        return [line for line in result.stdout.splitlines() if line]
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerPolicyError("could not check the rootless container limit") from exc


def _stop_run_containers(project_name: str) -> None:
    """Stop any run-owned containers left behind after the broker interrupts a container command."""
    try:
        result = subprocess.run(["podman", "ps", "--all", "--quiet", "--filter",
                                 f"label=aiflow4.run={project_name}"], capture_output=True, text=True, timeout=30)
        ids = [item for item in result.stdout.splitlines() if item] if result.returncode == 0 else []
        if ids:
            subprocess.run(["podman", "stop", "--time", "1", *ids], capture_output=True, timeout=30)
            subprocess.run(["podman", "rm", "--force", "--volumes", *ids], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _graphroot() -> Path:
    info = subprocess.run(["podman", "info", "--format", "json"], capture_output=True, text=True, timeout=20)
    if info.returncode:
        raise ContainerPolicyError("rootless Podman is unavailable")
    graphroot = Path(json.loads(info.stdout)["store"]["graphRoot"])
    return graphroot


def _directory_size(path: Path) -> int:
    try:
        result = subprocess.run(["du", "-s", "--bytes", str(path)], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return TOTAL_STORAGE_LIMIT + 1


def _worktree_storage_size(path: Path) -> int:
    """Return allocated bytes in a worktree, failing closed if its usage cannot be measured."""
    try:
        result = subprocess.run(["du", "-s", "--block-size=1", str(path)], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return WORKTREE_STORAGE_LIMIT + 1


def _worktree_storage_limit_reached(path: Path) -> bool:
    try:
        return (_worktree_storage_size(path) >= WORKTREE_STORAGE_LIMIT
                or shutil.disk_usage(path).free <= STORAGE_DISK_RESERVE)
    except OSError:
        return True


def _broker_invocation(worktree: Path, engine_home: str) -> tuple[list[str], str]:
    """Build an isolated launch for the installed broker, with no import path through a project checkout."""
    root = worktree.resolve(strict=True)
    module_file = Path(__file__).resolve(strict=True)
    if _within(module_file, root):
        raise RuntimeError("safe-container-runtime broker code must be installed outside the project worktree")
    trusted_root = module_file.parents[3]
    home = Path(engine_home).resolve(strict=True)
    if _within(home, root):
        raise RuntimeError("safe-container-runtime needs a trusted engine working directory")
    bootstrap = (
        "import importlib, os, pathlib, sys; "
        f"worktree=pathlib.Path({str(root)!r}); expected=pathlib.Path({str(module_file)!r}); "
        "definitely_under=lambda value: (lambda path: path == worktree or worktree in path.parents)("
        "pathlib.Path(value or os.getcwd()).resolve()); "
        "sys.path[:]=[entry for entry in sys.path if not definitely_under(entry)]; "
        f"sys.path.insert(0, {str(trusted_root)!r}); "
        "module=importlib.import_module('aiflow.v4.worker.containers'); "
        "assert pathlib.Path(module.__file__).resolve() == expected; "
        "raise SystemExit(module._broker_main())"
    )
    return [sys.executable, "-I", "-c", bootstrap, "--broker"], str(home)


def _execute(argv: list[str], cwd: Path, worktree: Path, *, runtime_dir: Path,
             project_name: str, stop_event: threading.Event | None = None) -> tuple[int, str, str]:
    lock_path = Path.home() / ".cache" / "aiflow4" / "container-runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _execute_locked(argv, cwd, worktree, runtime_dir=runtime_dir, project_name=project_name,
                                   stop_event=stop_event)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _execute_locked(argv: list[str], cwd: Path, worktree: Path, *, runtime_dir: Path,
                    project_name: str, stop_event: threading.Event | None = None) -> tuple[int, str, str]:
    root, run_cwd = worktree.resolve(strict=True), cwd.resolve(strict=True)
    if not _within(run_cwd, root):
        raise ContainerPolicyError("container working directory must stay inside the worktree")
    backend = "podman"
    if argv and argv[0] == "build":
        podman_argv = _build_argv(argv, root, run_cwd)
        compose_files: list[Path] = []
    elif argv and argv[0] == "run":
        podman_argv = _run_argv(argv, root, run_cwd)
        compose_files = []
    elif argv and argv[0] == "compose":
        podman_argv, compose_files, _ = _compose_command(argv, root, run_cwd, runtime_dir, project_name)
        backend = "podman-compose"
    else:
        raise ContainerPolicyError("only docker build, compose up/run, and mounted docker run are supported")
    if argv[0] == "run":
        podman_argv[1:1] = ["--label", f"aiflow4.run={project_name}"]
    graphroot = _graphroot()
    usage = _directory_size(graphroot)
    if usage >= IMAGE_CACHE_LIMIT:
        subprocess.run(["podman", "image", "prune", "--all", "--filter", f"until={IMAGE_PRUNE_AGE}", "--force"],
                       capture_output=True, timeout=300)
        usage = _directory_size(graphroot)
    if usage >= IMAGE_CACHE_LIMIT:
        raise ContainerPolicyError("container image cache reached its enforced storage limit")
    if shutil.disk_usage(graphroot).free < TOTAL_STORAGE_LIMIT - usage + 4 * 1024**3:
        subprocess.run(["podman", "image", "prune", "--all", "--filter", f"until={IMAGE_PRUNE_AGE}", "--force"],
                       capture_output=True, timeout=300)
        usage = _directory_size(graphroot)
        if shutil.disk_usage(graphroot).free < TOTAL_STORAGE_LIMIT - usage + 4 * 1024**3:
            raise ContainerPolicyError("container runtime stopped before its storage limit and disk reserve")
    worktree_usage = _worktree_storage_size(root)
    if worktree_usage >= WORKTREE_STORAGE_LIMIT:
        raise ContainerPolicyError("container worktree reached its enforced storage limit")
    if shutil.disk_usage(root).free < WORKTREE_STORAGE_LIMIT - worktree_usage + STORAGE_DISK_RESERVE:
        raise ContainerPolicyError("worktree filesystem cannot preserve its storage limit and disk reserve")
    command = ([backend, *podman_argv] if backend == "podman-compose" else ["podman", *podman_argv])
    proc = subprocess.Popen(command, cwd=str(run_cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    selector = selectors.DefaultSelector()
    if proc.stdout:
        selector.register(proc.stdout, selectors.EVENT_READ)
    output = bytearray()
    try:
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
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
                _stop_run_containers(project_name)
                output.extend(b"container command stopped because its AI run ended\n")
                return 125, output.decode(errors="replace"), ""
            if (_directory_size(graphroot) >= IMAGE_CACHE_LIMIT
                    or shutil.disk_usage(graphroot).free <= STORAGE_DISK_RESERVE):
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
                _stop_run_containers(project_name)
                output.extend(b"aiflow container storage cap reached; command stopped\n")
                return 125, output.decode(errors="replace"), ""
            if _worktree_storage_limit_reached(root):
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
                _stop_run_containers(project_name)
                output.extend(b"aiflow container worktree storage cap reached; command stopped\n")
                return 125, output.decode(errors="replace"), ""
            for ready, _ in selector.select(timeout=0.2):
                chunk = os.read(ready.fd, 65536)
                if chunk:
                    output.extend(chunk)
                    if len(output) > 16 * 1024 * 1024:
                        del output[:len(output) - 16 * 1024 * 1024]
        if proc.stdout:
            while chunk := os.read(proc.stdout.fileno(), 65536):
                output.extend(chunk)
                if len(output) > 16 * 1024 * 1024:
                    del output[:len(output) - 16 * 1024 * 1024]
        return proc.returncode or 0, output.decode(errors="replace"), ""
    finally:
        selector.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            _stop_run_containers(project_name)


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _BrokerServer = self.server  # type: ignore[assignment]
        try:
            creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid, peer_uid, _ = struct.unpack("3i", creds)
            raw = self.rfile.readline(128 * 1024)
            request = json.loads(raw)
            if request.get("action") == "bind":
                if peer_uid != 0 or not isinstance(request.get("pid"), int):
                    raise ContainerPolicyError("only the worker supervisor can bind a run process")
                server.bind_run(request["pid"])
                response = {"ok": True}
            elif request.get("action") == "shutdown":
                if peer_uid != 0:
                    raise ContainerPolicyError("only the worker supervisor can stop this broker")
                response = {"ok": True}
                server.stop_requested.set()
                server.cancel_commands.set()
                def shutdown() -> None:
                    try:
                        server.commands_idle.wait()
                        server.cleanup()
                    finally:
                        server.shutdown()
                threading.Thread(target=shutdown, daemon=True).start()
            else:
                if not secrets.compare_digest(str(request.get("token", "")), server.token):
                    raise ContainerPolicyError("container broker token is invalid")
                if not _is_child_of(peer_pid, server.allowed_root_pid):
                    raise ContainerPolicyError("container command is not from this run")
                if server.storage_exceeded.is_set():
                    raise ContainerPolicyError("this run reached its worktree storage limit")
                argv = request.get("argv")
                if not isinstance(argv, list) or any(not isinstance(item, str) or "\x00" in item for item in argv):
                    raise ContainerPolicyError("container argv is invalid")
                cwd = Path(str(request.get("cwd") or ""))
                if not _within(cwd, server.worktree):
                    raise ContainerPolicyError("container working directory must stay inside the worktree")
                if argv and argv[0] == "compose":
                    server.compose_used = True
                server.begin_command()
                try:
                    result, stdout, stderr = _execute([str(item) for item in argv], cwd, server.worktree,
                                                      runtime_dir=server.runtime_dir, project_name=server.project_name,
                                                      stop_event=server.cancel_commands)
                finally:
                    server.end_command()
                response = {"ok": True, "code": result, "stdout": stdout, "stderr": stderr}
        except ContainerPolicyError as exc:
            response = {"ok": False, "code": 125, "error": str(exc)}
        except Exception as exc:  # keep paths, command output, and environment values out of broker errors
            response = {"ok": False, "code": 125, "error": f"container runtime failed ({type(exc).__name__})"}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())


class _BrokerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, worktree: Path, runtime_dir: Path, project_name: str, token: str):
        self.worktree, self.runtime_dir, self.project_name, self.token = worktree, runtime_dir, project_name, token
        self.allowed_root_pid: int | None = None
        self.run_start_time: int | None = None
        self.bound_event = threading.Event()
        self.stop_requested = threading.Event()
        self.cancel_commands = threading.Event()
        self.storage_exceeded = threading.Event()
        self.commands_idle = threading.Event()
        self.commands_idle.set()
        self._commands_lock = threading.Lock()
        self._active_commands = 0
        self.compose_used = False
        self._cleanup_lock = threading.Lock()
        self._cleaned = False
        super().__init__(socket_path, _BrokerHandler)

    def bind_run(self, pid: int) -> None:
        identity = _proc_identity(pid)
        if identity is None:
            raise ContainerPolicyError("the AI run process is no longer available")
        self.allowed_root_pid, self.run_start_time = pid, identity
        self.bound_event.set()

    def begin_command(self) -> None:
        with self._commands_lock:
            self._active_commands += 1
            self.commands_idle.clear()

    def end_command(self) -> None:
        with self._commands_lock:
            self._active_commands -= 1
            if self._active_commands == 0:
                self.commands_idle.set()

    def cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            if not self.compose_used:
                return
            containers = subprocess.run(["podman", "ps", "-aq", "--filter", f"label=aiflow4.run={self.project_name}"],
                                        capture_output=True, text=True, timeout=30)
            ids = [item for item in containers.stdout.splitlines() if item] if containers.returncode == 0 else []
            if ids:
                subprocess.run(["podman", "stop", "--time", "1", *ids], capture_output=True, timeout=30)
                subprocess.run(["podman", "rm", "--force", "--volumes", *ids], capture_output=True, timeout=30)
            volumes = subprocess.run(["podman", "volume", "ls", "-q", "--filter",
                                      f"label=io.podman.compose.project={self.project_name}"],
                                     capture_output=True, text=True, timeout=30)
            names = [item for item in volumes.stdout.splitlines() if item] if volumes.returncode == 0 else []
            if names:
                subprocess.run(["podman", "volume", "rm", *names], capture_output=True, timeout=30)
            networks = subprocess.run(["podman", "network", "ls", "-q", "--filter",
                                       f"label=io.podman.compose.project={self.project_name}"],
                                      capture_output=True, text=True, timeout=30)
            names = [item for item in networks.stdout.splitlines() if item] if networks.returncode == 0 else []
            if names:
                subprocess.run(["podman", "network", "rm", *names], capture_output=True, timeout=30)


def _proc_identity(pid: int) -> int | None:
    """Return Linux's process start tick so PID reuse cannot keep an orphan broker alive."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _watch_run(server: _BrokerServer) -> None:
    """Enforce worktree storage limits and clean up if the worker or run exits unexpectedly."""
    while not server.bound_event.wait(0.25):
        if server.stop_requested.is_set():
            return
    pid, start_time = server.allowed_root_pid, server.run_start_time
    if pid is None or start_time is None:
        return
    while not server.stop_requested.wait(0.5):
        if _proc_identity(pid) != start_time:
            break
        if server.compose_used and not server.storage_exceeded.is_set() \
                and _worktree_storage_limit_reached(server.worktree):
            server.storage_exceeded.set()
            server.cancel_commands.set()
            server.commands_idle.wait()
            server.cleanup()
    if server.stop_requested.is_set():
        return
    server.cancel_commands.set()
    server.commands_idle.wait()
    try:
        server.cleanup()
    finally:
        server.shutdown()


def _is_child_of(pid: int, root_pid: int | None) -> bool:
    if not root_pid:
        return False
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        if pid == root_pid:
            return True
        seen.add(pid)
        try:
            status = Path(f"/proc/{pid}/stat").read_text()
            tail = status[status.rfind(")") + 2:].split()
            pid = int(tail[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def _send(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1800)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        with sock.makefile("rb") as stream:
            return json.loads(stream.readline(16 * 1024 * 1024))
    finally:
        sock.close()


def client_main() -> int:
    """Entry point for the run-local `docker` and `docker-compose` shims."""
    socket_path, token = os.environ.get("AIFLOW4_CONTAINER_SOCKET", ""), os.environ.get("AIFLOW4_CONTAINER_TOKEN", "")
    if not socket_path or not token:
        print("safe container runtime is not active for this run", file=sys.stderr)
        return 125
    argv = sys.argv[1:]
    if Path(sys.argv[0]).name == "docker-compose":
        argv = ["compose", *argv]
    try:
        response = _send(socket_path, {"token": token, "argv": argv, "cwd": os.getcwd()})
    except (OSError, ValueError, TimeoutError):
        print("safe container runtime broker is unavailable", file=sys.stderr)
        return 125
    if not response.get("ok"):
        print(str(response.get("error") or "container command denied"), file=sys.stderr)
        return int(response.get("code", 125))
    sys.stdout.write(str(response.get("stdout") or ""))
    sys.stderr.write(str(response.get("stderr") or ""))
    return int(response.get("code", 125))


def _broker_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", action="store_true")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    if os.geteuid() != _engine_identity()[0] or os.geteuid() == _identity(WORKER_USER)[0]:
        return 125
    worktree = Path(args.worktree).resolve(strict=True)
    if safe_container_runtime_version() == "missing":
        return 125
    server = _BrokerServer(args.socket, worktree, Path(args.runtime_dir), args.project, args.token)
    os.chmod(args.socket, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    threading.Thread(target=_watch_run, args=(server,), name="aiflow4-container-watchdog", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        Path(args.socket).unlink(missing_ok=True)
    return 0


class ContainerRuntime(AbstractContextManager):
    """Install per-run shims and start a rootless broker only for container-enabled dev/verify runs.

    The broker runs as the separate engine account. The run keeps `aiflow-worker`, which can reach only the shims
    and the broker socket, never Podman, its storage, or the broker's private directory.
    """
    def __init__(self, spec: RunSpec, run_as: list[str]):
        self.spec, self.run_as = spec, run_as
        run_id = secrets.token_hex(16)
        self.public_dir = scratch_root(spec) / "containers" / run_id
        self.socket_path = self.public_dir / "sock" / "broker.sock"
        self.runtime_dir = Path(_engine_identity()[2]) / "runs" / run_id
        self.token = secrets.token_urlsafe(32)
        self.project_name = "aiflow-" + secrets.token_hex(8)
        self.process: subprocess.Popen | None = None
        self.previous_env: dict[str, str | None] = {}
        self.acl_root: Path | None = None

    def _grant_worktree(self, root: Path) -> None:
        """Let the engine account bind-mount this one worktree; revoked again on exit."""
        result = subprocess.run(["setfacl", "-R", "-m", f"u:{ENGINE_USER}:rwX", "-m", f"d:u:{ENGINE_USER}:rwX",
                                 str(root)], capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError("could not grant the container engine access to the worktree")
        self.acl_root = root

    def __enter__(self):
        root = Path(self.spec.cwd).resolve(strict=True)
        if not self.run_as:
            raise RuntimeError("the container broker must run as the unprivileged engine user")
        if safe_container_runtime_version() == "missing":
            raise RuntimeError("safe-container-runtime is missing or unhealthy on this node")
        uid, engine_gid, home = _engine_identity()
        broker_argv, broker_cwd = _broker_invocation(root, home)
        worker_gid = _identity(WORKER_USER)[1]
        self.public_dir.mkdir(parents=True, exist_ok=False)
        self.public_dir.chmod(0o755)
        sock_dir = self.public_dir / "sock"
        sock_dir.mkdir()
        os.chown(sock_dir, uid, worker_gid)
        sock_dir.chmod(0o2770)
        self.runtime_dir.mkdir(parents=True, exist_ok=False)
        os.chown(self.runtime_dir.parent, uid, engine_gid)
        os.chown(self.runtime_dir, uid, engine_gid)
        self.runtime_dir.chmod(0o700)
        wrapper_dir = self.public_dir / "bin"
        wrapper_dir.mkdir()
        wrapper_dir.chmod(0o755)
        for name in ("docker", "docker-compose"):
            wrapper = wrapper_dir / name
            wrapper.write_text(f"#!{sys.executable}\nfrom aiflow.v4.worker.containers import client_main\nraise SystemExit(client_main())\n",
                               encoding="utf-8")
            wrapper.chmod(0o755)
        # A friendly message for scripts that call `podman` via PATH; the real boundary is the OS permission check.
        denied = wrapper_dir / "podman"
        denied.write_text(f"#!{sys.executable}\nimport sys\nprint('direct Podman access is disabled for AI runs', file=sys.stderr)\nraise SystemExit(125)\n",
                          encoding="utf-8")
        denied.chmod(0o755)
        self._grant_worktree(root)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home,
               "USER": ENGINE_USER, "LOGNAME": ENGINE_USER, "LANG": "C.UTF-8",
               "TMPDIR": str(self.runtime_dir), "PODMAN_COMPOSE_PROVIDER": shutil.which("podman-compose") or "podman-compose"}
        broker_argv.extend(["--socket", str(self.socket_path), "--worktree", str(root),
                            "--runtime-dir", str(self.runtime_dir), "--project", self.project_name,
                            "--token", self.token])
        self.process = subprocess.Popen(engine_prefix() + broker_argv, cwd=broker_cwd, env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError("safe-container-runtime broker did not start")
            try:
                test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                test.connect(str(self.socket_path))
                test.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.process.terminate()
            raise RuntimeError("safe-container-runtime broker did not become ready")
        shim_path = str(wrapper_dir) + os.pathsep + env["PATH"]
        for key in ("PATH", "AIFLOW4_CONTAINER_SOCKET", "AIFLOW4_CONTAINER_TOKEN", "AIFLOW4_CONTAINER_WORKTREE",
                    "DOCKER_HOST", "DOCKER_CONTEXT", "PODMAN_COMPOSE_PROVIDER"):
            self.previous_env[key] = self.spec.extra_env.get(key)
        self.spec.extra_env.update({"PATH": shim_path, "AIFLOW4_CONTAINER_SOCKET": str(self.socket_path),
                                    "AIFLOW4_CONTAINER_TOKEN": self.token, "AIFLOW4_CONTAINER_WORKTREE": str(root),
                                    # Absolute Docker clients cannot reach a machine daemon through their default socket.
                                    "DOCKER_HOST": "unix:///dev/null", "DOCKER_CONTEXT": "",
                                    "PODMAN_COMPOSE_PROVIDER": env["PODMAN_COMPOSE_PROVIDER"]})
        self.spec.container_start_hook = self.bind_process
        return self

    def bind_process(self, pid: int) -> None:
        response = _send(str(self.socket_path), {"action": "bind", "pid": int(pid)})
        if not response.get("ok"):
            raise RuntimeError("container broker rejected this AI run")

    def __exit__(self, exc_type, exc, tb):
        if self.process and self.process.poll() is None:
            try:
                _send(str(self.socket_path), {"action": "shutdown"})
                self.process.wait(timeout=15)
            except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        for key, value in self.previous_env.items():
            if value is None:
                self.spec.extra_env.pop(key, None)
            else:
                self.spec.extra_env[key] = value
        self.spec.container_start_hook = None
        if self.acl_root is not None:
            subprocess.run(["setfacl", "-R", "-x", f"u:{ENGINE_USER}", "-x", f"d:u:{ENGINE_USER}",
                            str(self.acl_root)], capture_output=True, timeout=120)
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        shutil.rmtree(self.public_dir, ignore_errors=True)
        return False


if __name__ == "__main__":
    raise SystemExit(_broker_main())
