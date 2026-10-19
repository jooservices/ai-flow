"""Run a project's configured post-merge command and safely summarize its output."""
from __future__ import annotations

import os
import re
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
        process_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        environment: Mapping[str, str] | None = None) -> DeployResult:
    """Run a configured shell command, persist redacted output, and return a short redacted tail."""
    env = os.environ if environment is None else environment
    timed_out = False
    exit_code: int | None
    try:
        completed = process_runner(command, shell=True, cwd=cwd, capture_output=True, text=True,
                                   timeout=timeout_minutes * 60)
        output = _text(completed.stdout)
        stderr = _text(completed.stderr)
        if stderr:
            output += ("\n" if output and not output.endswith("\n") else "") + stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        exit_code = None
        output = _text(error.stdout)
        stderr = _text(error.stderr)
        if stderr:
            output += ("\n" if output and not output.endswith("\n") else "") + stderr
        output += ("\n" if output and not output.endswith("\n") else "") + f"Command timed out after {timeout_minutes} minutes."
    except OSError as error:
        exit_code = 127
        output = f"Could not start deploy command ({type(error).__name__})."

    output = _redact(output, env)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"deploy-{stamp}-{time.time_ns()}.log"
    log_path.write_text(output + ("\n" if output and not output.endswith("\n") else ""), encoding="utf-8")
    return DeployResult(exit_code, timed_out, _tail(output), log_path)


def comment(result: DeployResult, timeout_minutes: int, log_reference: str) -> str:
    """Build a ticket-safe result comment. The command and environment are deliberately omitted."""
    if result.timed_out:
        status = f"timed out after {timeout_minutes} minute(s) (exit code unavailable; process terminated)"
    else:
        status = f"exit code {result.exit_code}"
    return (f"AI Flow post-merge command: {status}.\n"
            f"Log: {log_reference}\n"
            f"Output tail:\n{result.tail}")
