"""Node-local coordination and process priority for worker-run project checks."""
from __future__ import annotations

import fcntl
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

DEFAULT_TEST_SLOTS = 2
MAX_TEST_SLOTS = 32
TEST_NICE_VALUE = 10
SLOT_DIRECTORY = Path("/run/aiflow4-test-slots")


def slot_limit(directory: Path) -> int:
    """Read a validated limit, defaulting safely for old or incomplete node state."""
    try:
        value = int((directory / "limit").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return DEFAULT_TEST_SLOTS
    return value if 1 <= value <= MAX_TEST_SLOTS else DEFAULT_TEST_SLOTS


def _effective_directory() -> Path:
    override = os.environ.get("AIFLOW4_TEST_SLOT_DIR")
    if override:
        return Path(override)
    try:
        SLOT_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o3770)
    except PermissionError:
        return Path(tempfile.gettempdir()) / f"aiflow4-test-slots-{os.getuid()}"
    if os.access(SLOT_DIRECTORY, os.W_OK):
        return SLOT_DIRECTORY
    return Path(tempfile.gettempdir()) / f"aiflow4-test-slots-{os.getuid()}"


def _available_slot(directory: Path) -> tuple[int, TextIO | None]:
    occupied, available = 0, None
    for index in range(MAX_TEST_SLOTS):
        handle = (directory / f"slot-{index}.lock").open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            occupied += 1
            handle.close()
            continue
        if available is None:
            available = handle
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
    return occupied, available


def _acquire_slot(directory: Path) -> TextIO:
    guard_path = directory / "acquire.lock"
    with guard_path.open("a+") as guard:
        while True:
            fcntl.flock(guard, fcntl.LOCK_EX)
            occupied, available = _available_slot(directory)
            if available is not None and occupied < slot_limit(directory):
                fcntl.flock(guard, fcntl.LOCK_UN)
                return available
            if available is not None:
                fcntl.flock(available, fcntl.LOCK_UN)
                available.close()
            fcntl.flock(guard, fcntl.LOCK_UN)
            time.sleep(0.1)


@contextmanager
def suite_slot(directory: Path | None = None) -> Iterator[None]:
    """Wait for one of this node's configured suite slots and hold it until the check exits."""
    shared = directory or _effective_directory()
    shared.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = _acquire_slot(shared)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def nice_prefix(run_as: list[str]) -> list[str]:
    """Prefix a check process with nice 10 before dropping to the worker identity."""
    return ["nice", "-n", str(TEST_NICE_VALUE), *run_as]
