"""Run a project's configured post-merge command and safely summarize its output."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

SECRET_ENV_NAME = re.compile(
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|PRIVATE_KEY|API_KEY|ACCESS_KEY|AUTHORIZATION|"
    r"(?:^|_)PASS(?:_|$)|(?:^|_)KEY(?:_|$)|COOKIE)", re.I)


@dataclass(frozen=True)
class DeployResult:
    exit_code: int | None
    timed_out: bool
    tail: str
    log_path: Path
    interrupted: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.exit_code == 0


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _redact(text: str, environment: Mapping[str, str]) -> str:
    values = {value for name, value in environment.items() if SECRET_ENV_NAME.search(name) and value}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def _tail(text: str, max_lines: int = 20, max_chars: int = 2000) -> str:
    lines = text.rstrip().splitlines()[-max_lines:]
    result = "\n".join(lines)
    return result[-max_chars:] if result else "(no output)"


def run(command: str, cwd: Path, timeout_minutes: int, log_dir: Path,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        environment: Mapping[str, str] | None = None, *, heartbeat_seconds: int = 20,
        heartbeat: Callable[[], bool] | None = None) -> DeployResult:
    """Run a configured shell command, supervise its process group, and persist redacted output."""
    env = os.environ if environment is None else environment
    timed_out = False
    interrupted = False
    exit_code: int | None = None
    output: str | bytes | None = ""
    process: subprocess.Popen | None = None
    try:
        process = process_factory(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        deadline = time.monotonic() + timeout_minutes * 60
        interval = max(0.01, heartbeat_seconds)
        next_heartbeat = time.monotonic() + interval
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                exit_code, output = _stop(process)
                break
            wait_for = min(remaining, max(0.01, next_heartbeat - now)) if heartbeat else remaining
            try:
                output, _ = process.communicate(timeout=wait_for)
                exit_code = process.returncode
                break
            except subprocess.TimeoutExpired as error:
                output = error.output
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    exit_code, output = _stop(process)
                    break
                if heartbeat and now >= next_heartbeat:
                    if not heartbeat():
                        interrupted = True
                        exit_code, output = _stop(process)
                        break
                    next_heartbeat = now + interval
    except OSError as error:
        if process is not None and process.poll() is None:
            _stop(process)
        exit_code = 127
        output = f"Could not start deploy command ({type(error).__name__})."
    except BaseException:
        if process is not None and process.poll() is None:
            _stop(process)
        raise

    output = _text(output)
    if timed_out:
        output += ("\n" if output and not output.endswith("\n") else "") + f"Command timed out after {timeout_minutes} minutes."
    output = _redact(output, env)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"deploy-{stamp}-{time.time_ns()}.log"
    log_path.write_text(output + ("\n" if output and not output.endswith("\n") else ""), encoding="utf-8")
    return DeployResult(exit_code, timed_out, _tail(output), log_path, interrupted)


def _stop(process: subprocess.Popen) -> tuple[int | None, str | bytes | None]:
    """Terminate the command's whole session, escalating if it does not stop promptly."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        output, _ = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return process.returncode, output


def comment(result: DeployResult, timeout_minutes: int, log_reference: str) -> str:
    """Build a ticket-safe result comment. The command and environment are deliberately omitted."""
    if result.timed_out:
        status = f"timed out after {timeout_minutes} minute(s) (exit code unavailable; process terminated)"
    else:
        status = f"exit code {result.exit_code}"
    if result.interrupted:
        status += " (stopped after the worker or project-lock heartbeat was lost)"
    return (f"AI Flow post-merge command: {status}.\n"
            f"Log: {log_reference}\n"
            f"Output tail:\n{result.tail}")
