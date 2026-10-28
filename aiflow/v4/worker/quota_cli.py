"""Read provider quota from built-in CLI status commands without sending an AI prompt."""
from __future__ import annotations

import json
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

COMMANDS = {
    # These are provider-owned, read-only TUI commands; never replace them with a natural-language prompt.
    "codex": "/status",
    "claude": "/usage",
}
MAX_OUTPUT = 256_000


def _clean_terminal(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", " ", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", " ", text)
    text = re.sub(r"\x1b[()][0-2A-Z]", " ", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    return text


def _window_names(provider: str) -> tuple[tuple[str, str], ...]:
    if provider == "codex":
        return (("five_hour", r"\b(?:5\s*[- ]?h(?:our)?|five\s*[- ]?hour|session)\b"),
                ("weekly", r"\b(?:weekly|7\s*[- ]?d(?:ay)?|seven\s*[- ]?day)\b"))
    if provider == "claude":
        return (("five_hour", r"\b(?:current\s+session|5\s*[- ]?hour|five\s*[- ]?hour)\b"),
                ("weekly", r"\b(?:current\s+week|weekly|7\s*[- ]?day|seven\s*[- ]?day)\b"))
    return ()


def _reset_at(segment: str, observed_at: datetime) -> str | None:
    absolute = re.search(r"\b(?:reset|resets)\s*(?:at|on|:)\s*(\d{10,13})\b", segment)
    if absolute:
        value = float(absolute.group(1))
        if len(absolute.group(1)) == 13:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    iso = re.search(r"\b(?:reset|resets)\s*(?:at|on|:)\s*(\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}"
                    r"(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?)", segment)
    if iso:
        try:
            value = datetime.fromisoformat(iso.group(1).replace("z", "+00:00"))
            return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()
        except ValueError:
            return None
    relative = re.search(r"\b(?:reset|resets)\s+(?:in|after)\s+((?:\d+(?:\.\d+)?\s*"
                         r"(?:days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\s*)+)", segment)
    if not relative:
        return None
    seconds = 0.0
    units = {"d": 86400, "day": 86400, "days": 86400, "h": 3600, "hr": 3600, "hrs": 3600,
             "hour": 3600, "hours": 3600, "m": 60, "min": 60, "mins": 60, "minute": 60,
             "minutes": 60, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|"
                                   r"seconds?|secs?|sec|s)\b", relative.group(1)):
        seconds += float(amount) * units[unit]
    if not seconds:
        return None
    base = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at.astimezone(UTC)
    return (base + timedelta(seconds=seconds)).isoformat()


def parse_quota_text(provider: str, text: str, observed_at: datetime | None = None) -> dict:
    """Extract only explicitly labeled percent values; ambiguous output is not treated as quota."""
    windows: dict[str, dict] = {}
    normalized = re.sub(r"\s+", " ", text).casefold()
    labels = [(name, match.start(), match.end()) for name, pattern in _window_names(provider)
              for match in re.finditer(pattern, normalized)]
    labels.sort(key=lambda item: item[1])
    for index, (name, start, _) in enumerate(labels):
        end = labels[index + 1][1] if index + 1 < len(labels) else min(len(normalized), start + 180)
        segment = normalized[start:end]
        percentage = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%", segment)
        if not percentage or name in windows:
            continue
        value = min(100.0, max(0.0, float(percentage.group(1)))) / 100
        nearby = segment[max(0, percentage.start() - 32):percentage.end() + 32]
        if re.search(r"\b(?:remaining|left|available)\b", nearby):
            value = 1 - value
        window = {"utilization": round(value, 6)}
        reset_at = _reset_at(segment, observed_at or datetime.now(UTC))
        if reset_at:
            window["resets_at"] = reset_at
        windows[name] = window
    return {"provider": provider, "windows": windows} if windows else {}


def _ready_for_slash_command(provider: str, raw: bytes) -> bool:
    """Do not send slash input unless the TUI has rendered its interactive prompt."""
    text = _clean_terminal(raw)
    if provider == "codex":
        return "›" in text or "What would you like to do?" in text
    if provider == "claude":
        return "❯" in text
    return False


def _stop_process_group(pid: int, grace_s: float = 0.5) -> None:
    """Bound CLI shutdown and clean up any child processes spawned by the TUI."""
    group_signal = True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        group_signal = False
    except PermissionError:
        group_signal = False
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    reaped = False
    deadline = time.monotonic() + grace_s
    while True:
        if not reaped:
            try:
                reaped = os.waitpid(pid, os.WNOHANG)[0] == pid
            except ChildProcessError:
                reaped = True
        if reaped and not group_signal:
            return
        if group_signal:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                if reaped:
                    return
                group_signal = False
            except PermissionError:
                group_signal = False
                if reaped:
                    return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    if group_signal:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if not reaped:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def read_quota(provider: str, account_home: str, run_as: list[str] | None = None,
               timeout_s: float = 25.0, start_delay_s: float = 1.0,
               response_wait_s: float = 4.0) -> tuple[dict, str, str | None]:
    """Open the provider TUI, issue one fixed read-only slash command, and return parsed quota and observation time."""
    command = COMMANDS.get(provider)
    if not command:
        return {}, f"no read-only quota command is supported for {provider}", None
    binary = "codex" if provider == "codex" else "claude"
    argv = [*(run_as or []), binary]
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": account_home,
           "USER": "aiflow-worker", "LANG": "C.UTF-8", "TERM": "xterm-256color", "NO_COLOR": "1"}
    if provider == "codex":
        env["CODEX_HOME"] = str(Path(account_home) / ".codex")
    else:
        env["CLAUDE_CONFIG_DIR"] = str(Path(account_home) / ".claude")
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - exercised through fake CLI integration tests
        try:
            os.chdir(account_home)
            os.execvpe(argv[0], argv, env)
        except Exception:
            os._exit(127)
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
        output = bytearray()
        started = time.monotonic()
        command_sent = False
        prompt_seen = False
        sent_at = 0.0
        while time.monotonic() - started < timeout_s and len(output) < MAX_OUTPUT:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    output.extend(os.read(master, min(8192, MAX_OUTPUT - len(output))))
                except OSError:
                    break
            elapsed = time.monotonic() - started
            if not command_sent and elapsed >= start_delay_s and _ready_for_slash_command(provider, output):
                prompt_seen = True
                try:
                    os.write(master, (command + "\r").encode())
                except OSError:
                    break
                command_sent, sent_at = True, time.monotonic()
            elif command_sent and time.monotonic() - sent_at >= response_wait_s:
                break
        observed_at = datetime.now(UTC) if command_sent else None
        # /exit is also a fixed TUI command; SIGINT is a bounded fallback for older CLI versions.
        if command_sent:
            try:
                os.write(master, b"/exit\r")
                time.sleep(0.25)
            except OSError:
                pass
        _stop_process_group(pid)
        parsed = parse_quota_text(provider, _clean_terminal(bytes(output)), observed_at or datetime.now(UTC))
        if parsed:
            return parsed, "", observed_at.isoformat() if observed_at else None
        if not command_sent:
            if prompt_seen:
                return {}, "provider CLI exited before its quota command could be sent", None
            return {}, "provider CLI did not show its interactive prompt; no quota command was sent", None
        return {}, "provider CLI output did not contain a recognized quota window", None
    except (OSError, ValueError, TimeoutError) as exc:
        _stop_process_group(pid, grace_s=0)
        return {}, f"quota CLI failed: {type(exc).__name__}", None
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def read_quota_isolated(provider: str, account_home: str, run_as: list[str] | None = None,
                        timeout_s: float = 25.0, start_delay_s: float = 1.0,
                        response_wait_s: float = 4.0) -> tuple[dict, str, str | None]:
    """Run the PTY-based CLI read in a one-thread helper and return when the CLI observation was made."""
    if provider not in COMMANDS:
        return {}, f"no read-only quota command is supported for {provider}", None
    request = json.dumps({"provider": provider, "account_home": account_home, "run_as": run_as or [],
                          "timeout_s": timeout_s, "start_delay_s": start_delay_s,
                          "response_wait_s": response_wait_s})
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8"}
    try:
        result = subprocess.run([sys.executable, "-m", "aiflow.v4.worker.quota_cli"], input=request,
                                capture_output=True, text=True, timeout=timeout_s + 10,
                                cwd=str(Path(__file__).resolve().parents[3]), env=env, check=False)
    except subprocess.TimeoutExpired:
        return {}, "quota CLI helper timed out", None
    except OSError as exc:
        return {}, f"quota CLI helper failed: {type(exc).__name__}", None
    if result.returncode:
        return {}, f"quota CLI helper exited with status {result.returncode}", None
    try:
        response = json.loads(result.stdout)
        quota, error = response.get("quota"), response.get("error")
        observed_at = response.get("observed_at")
        return (quota if isinstance(quota, dict) else {}, str(error or ""),
                str(observed_at) if observed_at else None)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {}, "quota CLI helper returned invalid output", None


def _main() -> None:
    try:
        request = json.load(sys.stdin)
        quota, error, observed_at = read_quota(request["provider"], request["account_home"], request.get("run_as"),
                                               float(request.get("timeout_s", 25)),
                                               float(request.get("start_delay_s", 1)),
                                               float(request.get("response_wait_s", 4)))
        json.dump({"quota": quota, "error": error, "observed_at": observed_at}, sys.stdout)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        json.dump({"quota": {}, "error": "invalid quota CLI helper request"}, sys.stdout)


if __name__ == "__main__":
    _main()
