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
import secrets
import shutil
import shlex
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from ..adapters.base import RunSpec, scratch_root
from . import container_client as _container_client
from . import container_commands as _container_commands
from . import container_compose as _container_compose
from . import container_exec as _container_exec
from .container_pin import _WorktreePin  # noqa: F401 - re-exported
from .container_policy import (ACTIVE_CONTAINER_LIMIT, _SAFE_NAME, ContainerPolicyError, _compose_bind_source,  # noqa: F401 - re-exported
                               _inside_directory, _inside_file, _normalize_mount, _short_compose_volume,
                               _validate_bind, _validate_compose_document, _within)

WORKER_USER = "aiflow-worker"
ENGINE_USER = "aiflow-containers"
RUNTIME_TOOL = "safe-container-runtime"
IMAGE_CACHE_LIMIT = 12 * 1024**3
TOTAL_STORAGE_LIMIT = 20 * 1024**3
WORKTREE_STORAGE_LIMIT = 4 * 1024**3
RUNTIME_DIRECTORY_STORAGE_LIMIT = 1 * 1024**3
STORAGE_DISK_RESERVE = 4 * 1024**3
WORKTREE_QUOTA_ROOT = Path("/srv/aiflow/work")
IMAGE_CACHE_PROJECT_ID = 2_000_000_001
_RUN_PROJECT_ID_BASE = 1_000_000_000
_RUN_PROJECT_ID_RANGE = 1_000_000_000
QUOTA_MARKER = Path("/run/aiflow4-container-quota-preflight.json")
QUOTA_LOCK = Path("/run/lock/aiflow4-container-quota-preflight.lock")
CONTAINER_LAYER_LIMIT = "1g"
BUILD_LIMIT = "2g"
IMAGE_PRUNE_AGE = "168h"


def _identity(user: str) -> tuple[int, int, str]:
    entry = pwd.getpwnam(user)
    return entry.pw_uid, entry.pw_gid, entry.pw_dir


def _engine_identity() -> tuple[int, int, str]:
    """The account that owns Podman and its storage; never the account AI runs execute as."""
    return _identity(ENGINE_USER)


def engine_prefix() -> list[str]:
    uid, gid, _ = _engine_identity()
    return ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--"]


HOST_ENGINE_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/var/run/podman/podman.sock",
                       "/run/podman/podman.sock")


def _worker_can_run_podman() -> bool:
    """True when the AI run's own account (with the groups a run gets) could execute Podman or reach an engine socket."""
    uid, gid, _ = _identity(WORKER_USER)
    # Same group handling as loop.RUN_AS, so a supplementary group that grants Podman access is seen here.
    prefix = ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups", "--"]
    try:
        probe = subprocess.run([*prefix, "podman", "--version"], capture_output=True, text=True, timeout=15)
        # 126/127: found but not executable, or not found for that account. Anything else is not proof of denial.
        if probe.returncode not in {126, 127}:
            return True
        engine_socket = f"/run/user/{_engine_identity()[0]}/podman/podman.sock"
        for path in (*HOST_ENGINE_SOCKETS, engine_socket):
            reach = subprocess.run([*prefix, "sh", "-c", 'test -S "$1" && test -w "$1"', "probe", path],
                                   capture_output=True, text=True, timeout=15)
            if reach.returncode == 0:
                return True
    except (OSError, subprocess.TimeoutExpired):
        return True
    return False


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


def _engine_podman_info() -> dict[str, Any] | None:
    info = _run_as_engine(["podman", "info", "--format", "json"], timeout=20)
    if info.returncode:
        return None
    data = json.loads(info.stdout)
    if ((data.get("host") or {}).get("security") or {}).get("rootless") is not True:
        return None
    return data


def _engine_storage_ready(data: dict[str, Any]) -> tuple[Path, Path] | None:
    graphroot_value = str((data.get("store") or {}).get("graphRoot") or "")
    if not graphroot_value:
        return None
    graphroot = Path(graphroot_value)
    if not graphroot.is_dir():
        return None
    if not _private_to_engine(graphroot) or not _private_to_engine(Path(_engine_identity()[2])):
        return None
    engine_home = Path(_engine_identity()[2])
    if not _project_quota_mount(graphroot) or not _project_quota_mount(engine_home):
        return None
    return graphroot, engine_home


def _engine_capacity_ready(graphroot: Path, engine_home: Path) -> bool:
    usage, runtime_usage = shutil.disk_usage(graphroot), shutil.disk_usage(engine_home)
    if usage.total < TOTAL_STORAGE_LIMIT + RUNTIME_DIRECTORY_STORAGE_LIMIT + STORAGE_DISK_RESERVE:
        return False
    if runtime_usage.total < RUNTIME_DIRECTORY_STORAGE_LIMIT + STORAGE_DISK_RESERVE:
        return False
    if runtime_usage.free < RUNTIME_DIRECTORY_STORAGE_LIMIT + STORAGE_DISK_RESERVE:
        return False
    return True


def _compose_provider_version() -> list[str] | None:
    provider = _run_as_engine(["podman-compose", "--version"], timeout=15)
    if provider.returncode:
        return None
    versions = (provider.stdout or provider.stderr).strip().splitlines()
    return versions or None


def _engine_container_runtime_version() -> str:
    """Check Podman and its storage from the engine account without probing worker UID transitions."""
    try:
        data = _engine_podman_info()
        if data is None:
            return "missing"
        namespaces = _run_as_engine(["podman", "unshare", "true"], timeout=20)
        if namespaces.returncode:
            return "missing"
        storage = _engine_storage_ready(data)
        if storage is None:
            return "missing"
        versions = _compose_provider_version()
        podman_version = str((data.get("version") or {}).get("Version") or "")
        if not podman_version or not versions:
            return "missing"
        if not _engine_capacity_ready(*storage):
            return "missing"
        return f"rootless Podman {podman_version}; {versions[0][:60]}; XFS project quota"
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return "missing"


def safe_container_runtime_version() -> str:
    """Report healthy only when Podman is healthy and worker runs cannot reach or execute its engine."""
    try:
        if _engine_identity()[0] == _identity(WORKER_USER)[0] or _worker_can_run_podman():
            return "missing"
    except (OSError, KeyError):
        return "missing"
    version = _engine_container_runtime_version()
    if version == "missing" or os.geteuid() != 0:
        return "missing"
    try:
        data = json.loads(_run_as_engine(["podman", "info", "--format", "json"], timeout=20).stdout)
        graphroot = Path(str((data.get("store") or {}).get("graphRoot") or ""))
        if not graphroot.is_dir() or not _prepare_storage_quotas(graphroot, WORKTREE_QUOTA_ROOT):
            return "missing"
        return version + "; hard storage quota; ACL"
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return "missing"


def _project_quota_mount(path: Path) -> bool:
    """True only when the path is on XFS with kernel project quotas enabled."""
    try:
        quota = subprocess.run(["findmnt", "-T", str(path), "-n", "-o", "FSTYPE,OPTIONS"], capture_output=True,
                               text=True, timeout=10)
        fields = (quota.stdout or "").strip().split(None, 1) if quota.returncode == 0 else []
        filesystem, options_text = fields if len(fields) == 2 else ("", "")
        return filesystem == "xfs" and bool(set(options_text.split(",")).intersection({"pquota", "prjquota"}))
    except (OSError, subprocess.TimeoutExpired):
        return False


def _quota_limit_arg(limit_bytes: int) -> str:
    if limit_bytes <= 0 or limit_bytes % 1024**3:
        raise ValueError("XFS project quota must be a positive whole number of GiB")
    return f"{limit_bytes // 1024**3}g"


def _set_project_limit(path: Path, project_id: int, limit_bytes: int) -> bool:
    if not path.is_dir() or not _project_quota_mount(path) or not shutil.which("xfs_quota"):
        return False
    try:
        command = f"limit -p bhard={_quota_limit_arg(limit_bytes)} {int(project_id)}"
        result = subprocess.run(["xfs_quota", "-x", "-c", command, str(path)], capture_output=True,
                                text=True, timeout=60)
        return result.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _configure_project_quota(path: Path, project_id: int, limit_bytes: int) -> bool:
    """Assign a directory tree to an XFS project and apply its kernel-enforced hard block limit."""
    if not path.is_dir() or not _project_quota_mount(path) or not shutil.which("xfs_quota"):
        return False
    try:
        target = shlex.quote(str(path))
        command = f"project -s -p {target} {int(project_id)}"
        result = subprocess.run(["xfs_quota", "-x", "-c", command, str(path)], capture_output=True,
                                text=True, timeout=60)
        return result.returncode == 0 and _set_project_limit(path, project_id, limit_bytes)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _clear_project_quota(path: Path, project_id: int, *, pass_fds: tuple[int, ...] = ()) -> None:
    """Remove a temporary project assignment and its hard limit after the run has stopped."""
    if not shutil.which("xfs_quota"):
        return
    target = shlex.quote(str(path))
    for command in (f"project -C -p {target} {int(project_id)}", f"limit -p bhard=0 {int(project_id)}"):
        try:
            subprocess.run(["xfs_quota", "-x", "-c", command, str(path)], capture_output=True, text=True,
                           timeout=60, pass_fds=pass_fds)
        except (OSError, subprocess.TimeoutExpired):
            return


def _new_project_id() -> int:
    return _RUN_PROJECT_ID_BASE + secrets.randbelow(_RUN_PROJECT_ID_RANGE)


def _probe_worktree_quota(root: Path) -> bool:
    """Exercise quota assignment on the worktree filesystem before the node advertises the runtime."""
    if os.geteuid() != 0 or not shutil.which("xfs_quota") or not _project_quota_mount(root):
        return False
    probe = Path(tempfile.mkdtemp(prefix=".aiflow-quota-probe-", dir=root))
    project_id = _new_project_id()
    try:
        return _configure_project_quota(probe, project_id, WORKTREE_STORAGE_LIMIT)
    finally:
        _clear_project_quota(probe, project_id)
        shutil.rmtree(probe, ignore_errors=True)


def _probe_runtime_directory_quota(engine_home: Path) -> bool:
    """Exercise a per-run quota on the filesystem that will hold engine runtime directories."""
    if os.geteuid() != 0 or not engine_home.is_dir() or not _project_quota_mount(engine_home):
        return False
    probe = Path(tempfile.mkdtemp(prefix=".aiflow-runtime-quota-probe-", dir=engine_home))
    project_id = _new_project_id()
    try:
        return _configure_project_quota(probe, project_id, RUNTIME_DIRECTORY_STORAGE_LIMIT)
    finally:
        _clear_project_quota(probe, project_id)
        shutil.rmtree(probe, ignore_errors=True)


def _acl_supported(root: Path) -> bool:
    """Exercise setfacl and default ACL support on the filesystem that holds project worktrees."""
    setfacl = shutil.which("setfacl")
    if not setfacl or not root.is_dir():
        return False
    try:
        engine_uid = _engine_identity()[0]
        probe = Path(tempfile.mkdtemp(prefix=".aiflow-acl-probe-", dir=root))
        try:
            grant = subprocess.run([setfacl, "-m", f"u:{engine_uid}:rwx", "-m", f"d:u:{engine_uid}:rwx",
                                    str(probe)], capture_output=True, timeout=15)
            if grant.returncode:
                return False
            revoke = subprocess.run([setfacl, "-x", f"u:{engine_uid}", "-x", f"d:u:{engine_uid}", str(probe)],
                                    capture_output=True, timeout=15)
            return revoke.returncode == 0
        finally:
            shutil.rmtree(probe, ignore_errors=True)
    except (OSError, KeyError, subprocess.TimeoutExpired):
        return False


def _quota_marker_data(graphroot: Path, worktree_root: Path) -> dict[str, Any] | None:
    try:
        graph_info, work_info = graphroot.stat(), worktree_root.stat()
        return {"version": 1, "graphroot": str(graphroot.resolve(strict=True)),
                "graphroot_device": graph_info.st_dev, "graphroot_inode": graph_info.st_ino,
                "worktree_root": str(worktree_root.resolve(strict=True)),
                "worktree_device": work_info.st_dev, "worktree_inode": work_info.st_ino}
    except OSError:
        return None


def _quota_preflight(graphroot: Path, worktree_root: Path) -> tuple[Path, dict[str, Any]] | None:
    if os.geteuid() != 0 or not shutil.which("xfs_quota") or not shutil.which("setfacl"):
        return None
    engine_home = Path(_engine_identity()[2])
    expected = _quota_marker_data(graphroot, worktree_root)
    if expected is None or not _project_quota_mount(graphroot) or not _project_quota_mount(worktree_root):
        return None
    if not _project_quota_mount(engine_home) or not _probe_runtime_directory_quota(engine_home):
        return None
    return engine_home, expected


def _write_quota_marker(expected: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".aiflow-container-quota-", dir=QUOTA_MARKER.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(expected, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, QUOTA_MARKER)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _configure_shared_quotas(graphroot: Path, worktree_root: Path, expected: dict[str, Any]) -> bool:
    try:
        marker = json.loads(QUOTA_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker = None
    if marker == expected:
        return (_set_project_limit(graphroot, IMAGE_CACHE_PROJECT_ID, IMAGE_CACHE_LIMIT)
                and _acl_supported(worktree_root))
    if not _configure_project_quota(graphroot, IMAGE_CACHE_PROJECT_ID, IMAGE_CACHE_LIMIT):
        return False
    if not _probe_worktree_quota(worktree_root) or not _acl_supported(worktree_root):
        return False
    _write_quota_marker(expected)
    return True


def _prepare_storage_quotas(graphroot: Path, worktree_root: Path) -> bool:
    """Configure shared quotas once per boot and probe each filesystem used by a run before advertising support."""
    preflight = _quota_preflight(graphroot, worktree_root)
    if preflight is None:
        return False
    _, expected = preflight
    try:
        QUOTA_LOCK.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(QUOTA_LOCK, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        with os.fdopen(lock_fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _configure_shared_quotas(graphroot, worktree_root, expected)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


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


def _run_argv(argv: list[str], root: Path, cwd: Path, *, map_source=None) -> list[str]:
    return _container_commands.run_argv(
        argv, root, cwd, map_source=map_source, normalize_mount=_normalize_mount,
        active_containers=_active_containers, active_limit=ACTIVE_CONTAINER_LIMIT,
        layer_limit=CONTAINER_LAYER_LIMIT)


def _build_argv(argv: list[str], root: Path, cwd: Path, *, map_path=None) -> list[str]:
    return _container_commands.build_argv(
        argv, root, cwd, map_path=map_path, inside_file=_inside_file,
        inside_directory=_inside_directory, build_limit=BUILD_LIMIT)


def _compose_snapshot(data: dict[str, Any], compose_file: Path, project_dir: Path, root: Path,
                     runtime_dir: Path, map_worktree_path=None) -> Path:
    return _container_compose.compose_snapshot(
        data, compose_file, project_dir, root, runtime_dir, map_worktree_path=map_worktree_path,
        inside_file=_inside_file, inside_directory=_inside_directory, engine_identity=_engine_identity,
        compose_bind_source=_compose_bind_source, validate_bind=_validate_bind)


def _compose_env_snapshot(env_files: list[Path], project_dir: Path, runtime_dir: Path,
                          read_worktree_file) -> Path:
    return _container_compose.compose_env_snapshot(
        env_files, project_dir, runtime_dir, read_worktree_file, engine_identity=_engine_identity)


def _compose_command(argv: list[str], root: Path, cwd: Path, runtime_dir: Path,
                     project_name: str, *, map_worktree_path=None,
                     read_worktree_file=None) -> tuple[list[str], list[Path], str]:
    return _container_compose.compose_command(
        argv, root, cwd, runtime_dir, project_name, map_worktree_path=map_worktree_path,
        read_worktree_file=read_worktree_file, inside_file=_inside_file, inside_directory=_inside_directory,
        validate_document=_validate_compose_document, within=_within, active_containers=_active_containers,
        compose_snapshot=_compose_snapshot, env_snapshot=_compose_env_snapshot, engine_identity=_engine_identity,
        container_layer_limit=CONTAINER_LAYER_LIMIT, build_limit=BUILD_LIMIT)


def _active_containers() -> list[str]:
    try:
        result = subprocess.run(["podman", "ps", "--all", "--quiet"], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ContainerPolicyError("could not check the rootless container limit")
        return [line for line in result.stdout.splitlines() if line]
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerPolicyError("could not check the rootless container limit") from exc


def _podman_ids(argv: list[str], timeout: int = 30) -> list[str]:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return [item for item in result.stdout.splitlines() if item] if result.returncode == 0 else []


def _remove_containers(container_ids: list[str]) -> None:
    if not container_ids:
        return
    subprocess.run(["podman", "stop", "--time", "1", *container_ids], capture_output=True, timeout=30)
    subprocess.run(["podman", "rm", "--force", "--volumes", *container_ids], capture_output=True, timeout=30)


def _compose_resource_names(resource: str, project_name: str) -> list[str]:
    result = subprocess.run(["podman", resource, "ls", "-q", "--filter",
                             f"label=io.podman.compose.project={project_name}"],
                            capture_output=True, text=True, timeout=30)
    return [item for item in result.stdout.splitlines() if item] if result.returncode == 0 else []


def _remove_compose_resource(resource: str, project_name: str) -> None:
    names = _compose_resource_names(resource, project_name)
    if names:
        subprocess.run(["podman", resource, "rm", *names], capture_output=True, timeout=30)


def _cleanup_compose_containers(project_name: str) -> None:
    container_ids = _podman_ids(["podman", "ps", "-aq", "--filter", f"label=aiflow4.run={project_name}"])
    _remove_containers(container_ids)


def _stop_run_containers(project_name: str) -> None:
    """Stop any run-owned containers left behind after the broker interrupts a container command."""
    try:
        container_ids = _podman_ids(["podman", "ps", "--all", "--quiet", "--filter",
                                    f"label=aiflow4.run={project_name}"])
        _remove_containers(container_ids)
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


def _runtime_storage_limit_reached() -> bool:
    """Check shared Podman image/layer/volume usage while both commands and detached services are active."""
    try:
        graphroot = _graphroot()
        return (_directory_size(graphroot) >= IMAGE_CACHE_LIMIT
                or shutil.disk_usage(graphroot).free <= STORAGE_DISK_RESERVE)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return True


def _broker_invocation(worktree: Path, engine_home: str) -> tuple[list[str], str]:
    return _container_client.broker_invocation(worktree, engine_home, __file__, _within)


def _execute(argv: list[str], cwd: Path, worktree: Path, *, runtime_dir: Path,
             project_name: str, stop_event: threading.Event | None = None,
             worktree_pin: _WorktreePin | None = None) -> tuple[int, str, str]:
    lock_path = Path.home() / ".cache" / "aiflow4" / "container-runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mapped_fds = worktree_pin._descendant_snapshot() if worktree_pin else set()
        try:
            return _execute_locked(argv, cwd, worktree, runtime_dir=runtime_dir, project_name=project_name,
                                   stop_event=stop_event, worktree_pin=worktree_pin)
        finally:
            try:
                _cleanup_compose_files(runtime_dir)
            finally:
                try:
                    if worktree_pin:
                        worktree_pin._close_descendants_after(mapped_fds)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)


def _cleanup_compose_files(runtime_dir: Path) -> None:
    """Remove broker-owned Compose snapshots and overrides after every command attempt."""
    try:
        temporary_files = tuple(runtime_dir.glob("compose-*.yaml"))
    except OSError:
        return
    for path in temporary_files:
        path.unlink(missing_ok=True)


def _execute_locked(argv: list[str], cwd: Path, worktree: Path, *, runtime_dir: Path,
                    project_name: str, stop_event: threading.Event | None = None,
                    worktree_pin: _WorktreePin | None = None) -> tuple[int, str, str]:
    return _container_exec.execute_locked(
        argv, cwd, worktree, runtime_dir=runtime_dir, project_name=project_name, stop_event=stop_event,
        worktree_pin=worktree_pin, build_argv=_build_argv, run_argv=_run_argv,
        compose_command=_compose_command, graphroot=_graphroot, directory_size=_directory_size,
        worktree_storage_size=_worktree_storage_size, disk_usage=shutil.disk_usage,
        worktree_storage_limit_reached=_worktree_storage_limit_reached,
        stop_run_containers=_stop_run_containers, cache_limit=IMAGE_CACHE_LIMIT,
        total_limit=TOTAL_STORAGE_LIMIT, worktree_limit=WORKTREE_STORAGE_LIMIT,
        disk_reserve=STORAGE_DISK_RESERVE, image_prune_age=IMAGE_PRUNE_AGE)


class _BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _BrokerServer = self.server  # type: ignore[assignment]
        try:
            creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid, peer_uid, _ = struct.unpack("3i", creds)
            raw = self.rfile.readline(128 * 1024)
            request = json.loads(raw)
            response = _dispatch_broker_request(server, peer_pid, peer_uid, request)
        except ContainerPolicyError as exc:
            response = {"ok": False, "code": 125, "error": str(exc)}
        except Exception as exc:  # keep paths, command output, and environment values out of broker errors
            response = {"ok": False, "code": 125, "error": f"container runtime failed ({type(exc).__name__})"}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())


def _shutdown_broker_after_commands(server: _BrokerServer) -> None:
    try:
        server.commands_idle.wait()
        server.cleanup()
    finally:
        server.shutdown()


def _handle_broker_bind(server: _BrokerServer, peer_uid: int, request: dict[str, Any]) -> dict[str, bool]:
    if peer_uid != 0 or not isinstance(request.get("pid"), int):
        raise ContainerPolicyError("only the worker supervisor can bind a run process")
    server.bind_run(request["pid"])
    return {"ok": True}


def _handle_broker_shutdown(server: _BrokerServer, peer_uid: int) -> dict[str, bool]:
    if peer_uid != 0:
        raise ContainerPolicyError("only the worker supervisor can stop this broker")
    server.stop_requested.set()
    server.cancel_commands.set()
    threading.Thread(target=_shutdown_broker_after_commands, args=(server,), daemon=True).start()
    return {"ok": True}


def _authorize_broker_command(server: _BrokerServer, peer_pid: int, peer_uid: int,
                              request: dict[str, Any]) -> None:
    if not secrets.compare_digest(str(request.get("token", "")), server.token):
        raise ContainerPolicyError("container broker token is invalid")
    if request.get("action") == "supervise":
        if peer_uid != 0:
            raise ContainerPolicyError("only the worker supervisor can start run services")
    elif not _is_child_of(peer_pid, server.allowed_root_pid):
        raise ContainerPolicyError("container command is not from this run")


def _validate_broker_command(server: _BrokerServer, request: dict[str, Any]) -> tuple[list[str], Path]:
    if not server.worktree_pin.is_current():
        server.worktree_replaced.set()
        server.storage_exceeded.set()
        raise ContainerPolicyError("container worktree path was replaced during the run")
    if server.storage_exceeded.is_set():
        if server.worktree_replaced.is_set():
            raise ContainerPolicyError("container worktree path was replaced during the run")
        raise ContainerPolicyError("this run reached its enforced container storage limit")
    argv = request.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise ContainerPolicyError("container argv is invalid")
    cwd = Path(str(request.get("cwd") or ""))
    if not _within(cwd, server.worktree):
        raise ContainerPolicyError("container working directory must stay inside the worktree")
    if argv and argv[0] == "compose":
        server.compose_used = True
    return argv, cwd


def _execute_broker_command(server: _BrokerServer, argv: list[str], cwd: Path) -> dict[str, Any]:
    server.begin_command()
    try:
        result, stdout, stderr = _execute([str(item) for item in argv], cwd, server.worktree,
                                          runtime_dir=server.runtime_dir, project_name=server.project_name,
                                          stop_event=server.cancel_commands, worktree_pin=server.worktree_pin)
    finally:
        server.end_command()
    return {"ok": True, "code": result, "stdout": stdout, "stderr": stderr}


def _dispatch_broker_command(server: _BrokerServer, peer_pid: int, peer_uid: int,
                             request: dict[str, Any]) -> dict[str, Any]:
    _authorize_broker_command(server, peer_pid, peer_uid, request)
    argv, cwd = _validate_broker_command(server, request)
    return _execute_broker_command(server, argv, cwd)


def _dispatch_broker_request(server: _BrokerServer, peer_pid: int, peer_uid: int,
                             request: dict[str, Any]) -> dict[str, Any]:
    if request.get("action") == "bind":
        return _handle_broker_bind(server, peer_uid, request)
    if request.get("action") == "shutdown":
        return _handle_broker_shutdown(server, peer_uid)
    return _dispatch_broker_command(server, peer_pid, peer_uid, request)


class _BrokerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, worktree: Path, runtime_dir: Path, project_name: str, token: str,
                 expected_worktree_identity: tuple[int, int] | None = None):
        self.worktree_pin = _WorktreePin(worktree, expected_worktree_identity)
        self.worktree = self.worktree_pin.path
        self.runtime_dir, self.project_name, self.token = runtime_dir, project_name, token
        self.allowed_root_pid: int | None = None
        self.run_start_time: int | None = None
        self.bound_event = threading.Event()
        self.stop_requested = threading.Event()
        self.cancel_commands = threading.Event()
        self.storage_exceeded = threading.Event()
        self.worktree_replaced = threading.Event()
        self.commands_idle = threading.Event()
        self.commands_idle.set()
        self._commands_lock = threading.Lock()
        self._active_commands = 0
        self.compose_used = False
        self._cleanup_lock = threading.Lock()
        self._cleaned = False
        super().__init__(socket_path, _BrokerHandler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.worktree_pin.close()

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
            _cleanup_compose_containers(self.project_name)
            _remove_compose_resource("volume", self.project_name)
            _remove_compose_resource("network", self.project_name)


def _proc_identity(pid: int) -> int | None:
    return _container_client.proc_identity(pid)


def _wait_for_run_binding(server: _BrokerServer) -> bool:
    while not server.bound_event.wait(0.25):
        if server.stop_requested.is_set():
            return False
    return True


def _stop_for_replaced_worktree(server: _BrokerServer) -> bool:
    worktree_pin = getattr(server, "worktree_pin", None)
    if not worktree_pin or worktree_pin.is_current():
        return False
    server.worktree_replaced.set()
    server.storage_exceeded.set()
    server.cancel_commands.set()
    server.commands_idle.wait()
    server.cleanup()
    server.shutdown()
    return True


def _check_run_storage(server: _BrokerServer) -> None:
    worktree_pin = getattr(server, "worktree_pin", None)
    if not server.compose_used or server.storage_exceeded.is_set():
        return
    storage_root = worktree_pin.proc_path if worktree_pin else server.worktree
    if not (_worktree_storage_limit_reached(storage_root) or _runtime_storage_limit_reached()):
        return
    server.storage_exceeded.set()
    server.cancel_commands.set()
    server.commands_idle.wait()
    server.cleanup()


def _watch_bound_run(server: _BrokerServer) -> None:
    while not server.stop_requested.wait(0.5):
        if _proc_identity(server.allowed_root_pid) != server.run_start_time:
            break
        if _stop_for_replaced_worktree(server):
            return
        _check_run_storage(server)
    if server.stop_requested.is_set():
        return
    server.cancel_commands.set()
    server.commands_idle.wait()
    try:
        server.cleanup()
    finally:
        server.shutdown()


def _watch_run(server: _BrokerServer) -> None:
    """Enforce worktree storage limits and clean up if the worker or run exits unexpectedly."""
    if not _wait_for_run_binding(server):
        return
    if server.allowed_root_pid is None or server.run_start_time is None:
        return
    # The supervisor binds first; the AI process may rebind when it starts.
    _watch_bound_run(server)


def _is_child_of(pid: int, root_pid: int | None) -> bool:
    return _container_client.is_child_of(pid, root_pid)


def _send(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _container_client.send(socket_path, payload)


def client_main() -> int:
    return _container_client.client_main(_send)


def _broker_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", action="store_true")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--worktree-device", type=int, required=True)
    parser.add_argument("--worktree-inode", type=int, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    if os.geteuid() != _engine_identity()[0] or os.geteuid() == _identity(WORKER_USER)[0]:
        return 125
    if _engine_container_runtime_version() == "missing":
        return 125
    server = _BrokerServer(args.socket, Path(args.worktree), Path(args.runtime_dir), args.project, args.token,
                           (args.worktree_device, args.worktree_inode))
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
        self.acl_fd = -1
        self.quota_id: int | None = None
        self.runtime_quota_id: int | None = None
        self.supervisor_bound = False

    def _setfacl(self, *args: str) -> subprocess.CompletedProcess:
        """Run setfacl on the pinned worktree directory only: through its descriptor, never the mutable path.

        `-P` keeps the recursive walk from following symlinks the run may have planted inside the tree.
        """
        return subprocess.run(["setfacl", "-R", "-P", *args, f"/proc/self/fd/{self.acl_fd}/."],
                              capture_output=True, timeout=120, pass_fds=[self.acl_fd])

    def _grant_worktree(self, root: Path, identity: tuple[int, int]) -> None:
        """Let the engine account bind-mount this one worktree; revoked again on exit."""
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != identity:
            os.close(fd)
            raise RuntimeError("container worktree changed while the runtime started")
        self.acl_fd = fd
        result = self._setfacl("-m", f"u:{ENGINE_USER}:rwX", "-m", f"d:u:{ENGINE_USER}:rwX")
        if result.returncode:
            raise RuntimeError("could not grant the container engine access to the worktree")
        self.acl_root = root

    def __enter__(self):
        try:
            return self._setup()
        except BaseException:
            # Python skips __exit__ when __enter__ raises: undo the ACL, broker and run directories here.
            self.__exit__(None, None, None)
            raise

    def _setup(self):
        root = Path(self.spec.cwd).resolve(strict=True)
        root_info = os.lstat(root)
        if not stat.S_ISDIR(root_info.st_mode):
            raise RuntimeError("container worktree must be a real directory")
        root_identity = (root_info.st_dev, root_info.st_ino)
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
        self.runtime_quota_id = _new_project_id()
        if not _configure_project_quota(self.runtime_dir, self.runtime_quota_id,
                                        RUNTIME_DIRECTORY_STORAGE_LIMIT):
            raise RuntimeError("could not apply the hard XFS quota to the container runtime directory")
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
        self._grant_worktree(root, root_identity)
        self.quota_id = _new_project_id()
        if not _configure_project_quota(root, self.quota_id, WORKTREE_STORAGE_LIMIT):
            raise RuntimeError("could not apply the hard XFS quota to the container worktree")
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home,
               "USER": ENGINE_USER, "LOGNAME": ENGINE_USER, "LANG": "C.UTF-8",
               "TMPDIR": str(self.runtime_dir), "PODMAN_COMPOSE_PROVIDER": shutil.which("podman-compose") or "podman-compose"}
        broker_argv.extend(["--socket", str(self.socket_path), "--worktree", str(root),
                            "--worktree-device", str(root_identity[0]), "--worktree-inode", str(root_identity[1]),
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

    def run_supervised(self, argv: list[str], cwd: str | Path) -> tuple[int, str, str]:
        """Run one container command for the worker supervisor, before or beside the AI process.

        It goes through the same broker policy, quotas and cleanup as any run command. The first call binds the
        supervisor, so the broker also cleans up if the worker dies before the AI process starts.
        """
        if not self.supervisor_bound:
            self.bind_process(os.getpid())
            self.supervisor_bound = True
        response = _send(str(self.socket_path), {"action": "supervise", "token": self.token,
                                                 "argv": list(argv), "cwd": str(cwd)})
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "container command denied"))
        return int(response.get("code", 125)), str(response.get("stdout") or ""), str(response.get("stderr") or "")

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
        if self.quota_id is not None and self.acl_fd >= 0:
            # Use the already pinned directory descriptor so cleanup cannot be redirected by a path swap.
            self._clear_run_quota()
        if self.acl_fd >= 0:
            try:
                # Revoke through the pinned descriptor, so a path swapped for a symlink cannot redirect this.
                self._setfacl("-x", f"u:{ENGINE_USER}", "-x", f"d:u:{ENGINE_USER}")
            finally:
                os.close(self.acl_fd)
                self.acl_fd = -1
        if self.runtime_quota_id is not None:
            _clear_project_quota(self.runtime_dir, self.runtime_quota_id)
            self.runtime_quota_id = None
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        shutil.rmtree(self.public_dir, ignore_errors=True)
        return False

    def _clear_run_quota(self) -> None:
        assert self.quota_id is not None and self.acl_fd >= 0
        descriptor_path = Path(f"/proc/self/fd/{self.acl_fd}/.")
        _clear_project_quota(descriptor_path, self.quota_id, pass_fds=(self.acl_fd,))
        self.quota_id = None


if __name__ == "__main__":
    raise SystemExit(_broker_main())
