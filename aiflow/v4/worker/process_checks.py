"""Bounded process output collection for worker-run project checks."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess


def communicate(proc: subprocess.Popen, timeout_s: int) -> tuple[str, int]:
    """Kill a timed-out check process group and bound output collection after the timeout."""
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            output, _ = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired as exc:
            output = exc.output or ""
            if proc.stdout is not None:
                proc.stdout.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1)
        output = f"{_output_text(output)}\nCheck exceeded {timeout_s} seconds."
    exit_code = int(proc.returncode or 0)
    return _output_text(output), (-signal.SIGKILL if timed_out and exit_code == 0 else exit_code)


def _output_text(output: str | bytes | None) -> str:
    """Normalize subprocess output, including timeout exceptions that retain bytes in text mode."""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""
