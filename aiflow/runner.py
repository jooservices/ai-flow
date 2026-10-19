"""Run one AI process, keep the lease alive, and stop it when it is idle or over time."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path


def head(workdir: Path) -> str:
    """Current commit of the worktree; '' when git is not installed (the shared runtimes have none) or this is no repository."""
    try:
        result = subprocess.run(["git", "-C", str(workdir), "rev-parse", "HEAD"], capture_output=True, text=True)
    except OSError:
        return ""
    return result.stdout.strip()


def run(argv: list[str], stdin_text: str | None, workdir: Path, log_file: Path, env: dict[str, str], *,
        idle_seconds: int, max_seconds: int, heartbeat_seconds: int, heartbeat: Callable[[], bool]) -> tuple[int | None, str]:
    """Return (exit code, why): why is ok | exit | idle | timeout | lost-lease."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=workdir, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        if stdin_text is not None:
            assert proc.stdin is not None
            proc.stdin.write(stdin_text.encode())
            proc.stdin.close()
        started = last_activity = last_beat = time.monotonic()
        seen_size, seen_head = 0, head(workdir)
        try:
            while True:
                try:
                    code = proc.wait(timeout=5)
                    return code, "ok" if code == 0 else "exit"
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                size, current = log_file.stat().st_size, head(workdir)
                if size != seen_size or current != seen_head:
                    seen_size, seen_head, last_activity = size, current, now
                if now - last_beat >= heartbeat_seconds:
                    last_beat = now
                    if not heartbeat():
                        return _stop(proc), "lost-lease"
                if now - last_activity > idle_seconds:
                    return _stop(proc), "idle"
                if now - started > max_seconds:
                    return _stop(proc), "timeout"
        except BaseException:  # never leave an AI running unsupervised, whatever went wrong here
            _stop(proc)
            raise


def _stop(proc: subprocess.Popen) -> int | None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return proc.poll()
