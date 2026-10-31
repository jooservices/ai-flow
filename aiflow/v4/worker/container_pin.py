"""The worktree pin of the container broker: one open directory identity, addressed through its descriptor.

Moved out of containers.py (JAIF-510) without changes; containers.py re-exports the name."""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from .container_policy import ContainerPolicyError


class _WorktreePin:
    """Keep an open directory identity and address its contents through that descriptor for the broker lifetime."""
    def __init__(self, path: Path, expected_identity: tuple[int, int] | None = None):
        self.path = Path(path).resolve(strict=True)
        info = os.lstat(self.path)
        if not stat.S_ISDIR(info.st_mode):
            raise ContainerPolicyError("container worktree must be a real directory")
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        pinned = os.fstat(self.fd)
        self.identity = (pinned.st_dev, pinned.st_ino)
        if ((info.st_dev, info.st_ino) != self.identity
                or expected_identity is not None and self.identity != expected_identity):
            os.close(self.fd)
            raise ContainerPolicyError("container worktree changed while the broker started")
        self.proc_path = Path(f"/proc/{os.getpid()}/fd/{self.fd}")
        self._descendant_fds: set[int] = set()
        self._fd_lock = threading.Lock()

    def is_current(self) -> bool:
        try:
            info = os.lstat(self.path)
            return (stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == self.identity
                    and (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino) == self.identity)
        except OSError:
            return False

    def assert_current(self) -> None:
        if not self.is_current():
            raise ContainerPolicyError("container worktree path was replaced during the run")

    def map_path(self, path: Path) -> Path:
        """Pin a descendant without following symlinks and return its stable proc-fd path.

        Validating with ``resolve`` and returning the original pathname leaves a swap window before Podman opens
        the path. Walk from the open worktree descriptor with O_NOFOLLOW instead; the child process then opens the
        same inode even if the untrusted run renames or replaces its directory entry.
        """
        self.assert_current()
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.path / candidate
        try:
            relative = candidate.relative_to(self.path)
        except ValueError as exc:
            raise ContainerPolicyError("container paths must stay inside the pinned worktree") from exc
        if ".." in relative.parts:
            raise ContainerPolicyError("container paths must stay inside the pinned worktree")
        if not relative.parts or relative == Path("."):
            return self.proc_path
        parent_fd = self.fd
        opened: list[int] = []
        try:
            for index, part in enumerate(relative.parts):
                if part in {"", ".", ".."}:
                    raise ContainerPolicyError("container paths must be literal descendants of the worktree")
                flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC
                if index < len(relative.parts) - 1:
                    flags |= os.O_DIRECTORY
                child_fd = os.open(part, flags, dir_fd=parent_fd)
                opened.append(child_fd)
                info = os.fstat(child_fd)
                if stat.S_ISLNK(info.st_mode):
                    raise ContainerPolicyError("container paths cannot traverse worktree symlinks")
                if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                    raise ContainerPolicyError("container path parent is not a directory")
                if index == len(relative.parts) - 1 and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ContainerPolicyError("container paths must be regular files or directories")
                parent_fd = child_fd
            with self._fd_lock:
                self._descendant_fds.update(opened)
            return Path(f"/proc/{os.getpid()}/fd/{opened[-1]}")
        except OSError as exc:
            for fd in opened:
                os.close(fd)
            raise ContainerPolicyError("container descendant changed or is unavailable") from exc
        except BaseException:
            for fd in opened:
                os.close(fd)
            raise

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        """Read a regular worktree file through descriptors opened from the pinned root without following links."""
        self.assert_current()
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.path / candidate
        try:
            relative = candidate.relative_to(self.path)
        except ValueError as exc:
            raise ContainerPolicyError("container files must stay inside the pinned worktree") from exc
        if not relative.parts or ".." in relative.parts:
            raise ContainerPolicyError("container files must stay inside the pinned worktree")

        parent_fd = self.fd
        opened: list[int] = []
        try:
            for index, part in enumerate(relative.parts):
                if part in {"", ".", ".."}:
                    raise ContainerPolicyError("container files must be literal descendants of the worktree")
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                if index < len(relative.parts) - 1:
                    flags |= os.O_DIRECTORY
                child_fd = os.open(part, flags, dir_fd=parent_fd)
                opened.append(child_fd)
                info = os.fstat(child_fd)
                if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                    raise ContainerPolicyError("container file parent is not a directory")
                if index == len(relative.parts) - 1 and not stat.S_ISREG(info.st_mode):
                    raise ContainerPolicyError("container files must be regular files")
                parent_fd = child_fd
            with os.fdopen(opened[-1], "r", encoding=encoding) as stream:
                opened.pop()
                return stream.read()
        except OSError as exc:
            raise ContainerPolicyError("container file changed or is unavailable") from exc
        finally:
            for fd in opened:
                os.close(fd)

    def close(self) -> None:
        with self._fd_lock:
            for fd in self._descendant_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._descendant_fds.clear()
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _descendant_snapshot(self) -> set[int]:
        with self._fd_lock:
            return set(self._descendant_fds)

    def _close_descendants_after(self, snapshot: set[int]) -> None:
        with self._fd_lock:
            new_fds = self._descendant_fds - snapshot
            self._descendant_fds.difference_update(new_fds)
            for fd in new_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
