"""Failure handling of the worktree pin: descriptors opened for one mapping are closed on every exit."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiflow.v4.core.broad_except import unexplained_count
from aiflow.v4.worker import container_pin


def test_interrupt_after_descendant_open_closes_its_descriptor_and_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the handler is explained and closes descriptors on interrupt."""
    assert unexplained_count(Path(container_pin.__file__).read_text(encoding="utf-8")) == 0

    worktree = tmp_path / "checkout"
    (worktree / "src").mkdir(parents=True)
    target = worktree / "src" / "marker"
    target.write_text("worktree data")
    pin = container_pin._WorktreePin(worktree)
    real_open, real_fstat = os.open, os.fstat
    opened: list[int] = []

    def recording_open(path: str, flags: int, *, dir_fd: int) -> int:
        """Record and return a descriptor opened by the mapping."""
        descriptor = real_open(path, flags, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def interrupted_fstat(descriptor: int) -> os.stat_result:
        """Raise an interrupt while inspecting any descendant descriptor."""
        if descriptor == pin.fd:
            return real_fstat(descriptor)
        raise KeyboardInterrupt

    try:
        monkeypatch.setattr(container_pin.os, "open", recording_open)
        monkeypatch.setattr(container_pin.os, "fstat", interrupted_fstat)
        with pytest.raises(KeyboardInterrupt):
            pin.map_path(target)
        assert opened, "the descendant descriptor must have been opened before the interrupt"
        for descriptor in opened:
            with pytest.raises(OSError):
                real_fstat(descriptor)
    finally:
        monkeypatch.undo()
        pin.close()
