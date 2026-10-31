"""Clean node worktrees, scratch files, and leftover test databases."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from ..worker.client import ControlClient
from ..worker.container_pin import _WorktreePin
from ..worker.container_policy import ContainerPolicyError

log = logging.getLogger("aiflow4.node")

MIN_WORK_DISK_FREE = 5 * 1024 ** 3

_last_sweep = 0.0

_WORKTREE_CLEANUP_IMAGE = "busybox:1.36.1"

_last_testdb_sweep = 0.0

_last_clean = 0.0

def _artifact_dirs_are_real(pin: _WorktreePin) -> bool:
    """True when artifacts/reports/e2e exist below the pinned directory as real directories (no symlink at any level).

    Every step is opened relative to the previous descriptor with O_NOFOLLOW, so a swap after this check cannot
    change which inode was inspected."""
    parent = pin.fd
    opened: list[int] = []
    try:
        for name in ("artifacts", "reports", "e2e"):
            parent = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            opened.append(parent)
        return True
    except OSError:
        return False
    finally:
        for fd in opened:
            os.close(fd)


def _remove_e2e_artifacts_as_root(worktree: Path, identity: tuple[int, int] | None = None) -> None:
    """Use a disposable root container to clear generated artifacts before host-side worktree removal.

    The container sees only this worktree and removes its artifacts directory, which is disposable because
    the worktree itself has already been approved for deletion. Removing the directory inside the container
    also handles root-owned, read-only directory entries that the node agent cannot chmod or unlink.

    The directory is pinned (JAIF-515): `identity` is the (device, inode) seen when the sweep scanned it, the open
    descriptor is checked against it, and the container mounts that descriptor instead of the path, so a run that
    replaces the directory (for example by a symlink) after the scan makes the cleanup refuse instead of letting
    root delete the artifacts of another directory."""
    try:
        pin = _WorktreePin(worktree, expected_identity=identity)
    except (ContainerPolicyError, OSError) as exc:
        raise OSError(f"worktree changed after the scan, root cleanup refused: {exc}") from exc
    try:
        if not _artifact_dirs_are_real(pin):
            return
        pin.assert_current()
        command = [
            "docker", "run", "--rm", "--pull=missing", "--user=0:0", "--network=none", "--read-only",
            "--cap-drop=ALL", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
            "--mount", f"type=bind,source={pin.proc_path},target=/worktree",
            _WORKTREE_CLEANUP_IMAGE, "rm", "-rf", "--", "/worktree/artifacts",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise OSError("root cleanup container timed out after 120 seconds") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit status {result.returncode}").strip().splitlines()
            raise OSError(f"root cleanup container failed: {detail[0][:240] if detail else result.returncode}")
    except ContainerPolicyError as exc:
        raise OSError(f"worktree changed during root cleanup: {exc}") from exc
    finally:
        pin.close()

def clean_worktrees(api: ControlClient, node: str, srv_root: str = "/srv/aiflow", min_age_s: int = 600) -> int:
    """Remove the worktrees of finished tickets (Done) and spent per-run checkouts: the control plane decides
    which directories can go. Runs at most every ten minutes; returns how many were removed."""
    global _last_sweep
    if time.time() - _last_sweep < 600:
        return 0
    _last_sweep = time.time()
    work = Path(srv_root) / "work"
    found: dict[str, Path] = {}
    identities: dict[str, tuple[int, int]] = {}
    for project in work.iterdir() if work.is_dir() else []:
        if not project.is_dir():
            continue
        for d in project.iterdir():
            try:
                if d.is_dir() and not d.is_symlink() and time.time() - d.stat().st_mtime > min_age_s:
                    found[f"{project.name}/{d.name}"] = d
                    info = d.lstat()
                    identities[f"{project.name}/{d.name}"] = (info.st_dev, info.st_ino)
            except OSError:
                pass
    if not found:
        return 0
    remove = api.call("POST", f"/v1/nodes/{node}/worktrees/sweep", {"dirs": sorted(found)}, retries=1).get("remove", [])
    removed = 0
    for entry in remove:
        path = found.get(entry)
        if path is None:
            continue
        try:
            _remove_e2e_artifacts_as_root(path, identities.get(entry))
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) != identities.get(entry):
                raise OSError("worktree changed after the scan, removal refused")
            for root, _dirs, _files in os.walk(path, topdown=True, followlinks=False):
                os.chmod(root, 0o700)  # read-only triage checkouts
            shutil.rmtree(path)
            removed += 1
        except OSError as exc:
            log.warning("could not remove worktree %s: %s", entry, exc)
    if removed:
        for mirror in (Path(srv_root) / "mirrors").glob("*"):
            subprocess.run(["git", "-c", "safe.directory=*", "-C", str(mirror), "worktree", "prune", "--expire=now"],
                           capture_output=True)
        log.info("removed %d worktrees of finished tickets", removed)
    return removed

def _disk_free_bytes() -> int:
    """Return free space on the filesystem used for AI Flow work."""
    return shutil.disk_usage("/srv/aiflow" if os.path.isdir("/srv/aiflow") else "/").free

def _run_disk_cleanup(cleanup, auto_fixed_conditions: set[str]) -> int:
    """Run cleanup and report disk-space as fixed only if it reclaims enough space to meet the minimum."""
    before = _disk_free_bytes()
    removed = cleanup()
    if not removed:
        return removed

    after = _disk_free_bytes()
    reclaimed = max(0, after - before)
    if before < MIN_WORK_DISK_FREE and reclaimed >= MIN_WORK_DISK_FREE - before:
        auto_fixed_conditions.add("disk-space")
    return removed

def _test_database_urls(projects_dir: str = "/etc/aiflow/projects") -> list[str]:
    """Test database URLs from the project env files on this node (never logged: they carry a password)."""
    urls: list[str] = []
    for f in sorted(Path(projects_dir).glob("*.env")):
        try:
            for line in f.read_text().splitlines():
                key, _, value = line.partition("=")
                value = value.strip().strip("'\"")
                if key.strip() == "AIFLOW4_TEST_DATABASE_URL" and value and value not in urls:
                    urls.append(value)
        except OSError:
            continue
    return urls

def sweep_test_databases(projects_dir: str = "/etc/aiflow/projects", every_s: int = 600) -> int:
    """Drop `aiflow4_t_*` test databases that no connection uses and that are over two hours old, at most every
    ten minutes. Returns how many were dropped."""
    global _last_testdb_sweep
    if time.time() - _last_testdb_sweep < every_s:
        return 0
    _last_testdb_sweep = time.time()
    from ..core import testdb
    dropped = 0
    for url in _test_database_urls(projects_dir):
        try:
            dropped += len(testdb.sweep(url))
        except Exception as e:  # noqa: BLE001 - the test server may be down; try again next time
            log.warning("test database sweep failed: %s", type(e).__name__)
    if dropped:
        log.info("dropped %s leftover test databases", dropped)
    return dropped

def clean_scratch(max_age_s: int = 6 * 3600) -> int:
    """Remove the AI user's temp files older than six hours from /tmp (RAM on the lab machines) and from the
    scratch tmp on disk. Runs at most once an hour; returns how many entries were removed."""
    global _last_clean
    if time.time() - _last_clean < 3600:
        return 0
    _last_clean = time.time()
    removed = 0
    try:
        import pwd
        uid = pwd.getpwnam("aiflow-worker").pw_uid
    except (KeyError, ImportError):
        return 0
    for base in (Path("/tmp"), Path("/srv/aiflow/cache/ai/tmp")):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            try:
                st = entry.lstat()
                if st.st_uid != uid or time.time() - st.st_mtime < max_age_s:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    if removed:
        log.info("cleaned %s old AI temp entries", removed)
    return removed
