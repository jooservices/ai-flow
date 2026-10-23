"""Adapters turn a run (prompt, worktree, account, model) into one AI CLI process and read its result.

All CLIs are run without a terminal, with the account's own HOME and config directory, and with output in
their JSON event format. The run never commits or pushes: the worker does that after the AI exits.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Shared with the current flow: phrases that mean "this AI cannot work now" (usage limit, balance, sign-in).
QUOTA_PATTERN = re.compile(
    r"usage limit (reached|exceeded|hit)|hit your (usage )?limit|rate.?limit(ed| exceeded| reached| hit)|"
    r"quota (exceeded|exhausted|reached)|exceeded (your )?(current )?quota|insufficient[_ ]quota|quota_exceeded|out of quota|"
    r"too many requests|credit balance|resets? (at|in) \d|"
    r"status 402|http 402|payment required|balance exhausted|usage balance", re.I)
AUTH_PATTERN = re.compile(r"not signed in|not logged in|api key has expired|api_key_expired|invalid api key|"
                          r"please log ?in|authentication_error|please run /login|login expired", re.I)
NETWORK_PATTERN = re.compile(r"network (unreachable|error)|could not resolve host|name or service not known|"
                             r"connection (refused|reset|timed out)|econn(reset|refused|unreachable)|"
                             r"enotfound|etimedout|socket hang up|network request failed|connection error|"
                             r"unable to connect|fetch failed|failed to connect|temporary failure in name resolution", re.I)
RESULT_LINE = re.compile(r"^(RESULT|VERDICT):\s*(DONE|PASS|FAIL|BLOCKED)\b[:\s-]*(.*)$", re.M)


@dataclass
class RunSpec:
    prompt: str
    cwd: str
    account_home: str
    model: str = ""
    effort: str = ""
    timeout_s: int = 3600
    idle_s: int = 900
    control_url: str = ""
    run_token: str = ""
    log_path: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    sandbox: str = "workspace-write"


@dataclass
class RunResult:
    exit_code: int | None = None
    final_text: str = ""
    verdict: str = ""          # DONE | PASS | FAIL | BLOCKED | ""
    verdict_reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    permission_denials: int = 0
    error_kind: str = ""       # "" | quota | auth | network | provider | timeout | idle | other
    error_message: str = ""    # the provider's own words, for people
    reset_at: datetime | None = None
    credential_expires_at: datetime | None = None
    quota: dict = field(default_factory=dict)
    duration_s: float = 0.0
    log_path: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["reset_at"] = self.reset_at.isoformat() if self.reset_at else None
        d["credential_expires_at"] = self.credential_expires_at.isoformat() if self.credential_expires_at else None
        return d


def _expiry_datetime(value) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def quota_from_event(event: dict, provider: str) -> dict:
    """Normalize quota/rate-limit objects some CLI versions include in their JSON event stream."""
    keys = {"rate_limits", "ratelimits", "quota", "quota_status", "usage_limits", "usage_limits_info"}
    found: list[dict] = []

    def visit(value: object, depth: int = 0) -> None:
        if depth > 5 or not isinstance(value, dict):
            return
        for key, child in value.items():
            if str(key).lower().replace("-", "_") in keys and isinstance(child, dict):
                found.append(child)
            if isinstance(child, dict):
                visit(child, depth + 1)

    visit(event)
    if not found:
        return {}
    source = found[-1]
    windows = source.get("windows") if isinstance(source.get("windows"), dict) else source
    aliases = {"primary": "five_hour", "secondary": "seven_day", "five_hour": "five_hour",
               "fivehour": "five_hour", "seven_day": "seven_day", "weekly": "seven_day"}
    normalized: dict[str, dict] = {}
    for raw_name, raw in windows.items():
        if not isinstance(raw, dict):
            continue
        name = aliases.get(str(raw_name).lower())
        if not name:
            continue
        utilization = next((raw[k] for k in ("utilization", "used_percent", "usedPercent", "utilization_percent")
                            if raw.get(k) is not None), None)
        if utilization is None:
            used, limit = raw.get("used_tokens"), raw.get("limit_tokens") or raw.get("limit") or raw.get("total")
            try:
                if used is not None and limit:
                    utilization = float(used) / float(limit)
            except (TypeError, ValueError, ZeroDivisionError):
                utilization = None
        if utilization is None:
            continue
        try:
            utilization = float(utilization)
            if utilization > 1:
                utilization /= 100
        except (TypeError, ValueError):
            continue
        item = {"utilization": utilization}
        reset = next((raw[k] for k in ("resets_at", "resetsAt", "reset_at", "resetAt") if raw.get(k) is not None), None)
        if reset is not None:
            item["resets_at"] = reset
        for key in ("quota_tokens", "limit_tokens", "total_tokens", "window_duration_minutes", "windowDurationMins"):
            if raw.get(key) is not None:
                item[key] = raw[key]
        if raw.get("limit") is not None and "limit_tokens" not in item:
            item["limit_tokens"] = raw["limit"]
        if raw.get("total") is not None and "total_tokens" not in item:
            item["total_tokens"] = raw["total"]
        duration = raw.get("window_duration_mins")
        if duration is not None:
            item["window_duration_minutes"] = duration
        normalized[name] = item
    return {"provider": provider, "windows": normalized} if normalized else {}


def _credential_expiry(event: dict, res: RunResult, credential_context: bool = False) -> None:
    """Capture only explicit expiry metadata emitted by a CLI's structured event stream."""
    if res.credential_expires_at:
        return
    for key, value in event.items():
        if key in ("credential_expires_at", "credentialExpiresAt") or (credential_context and key in ("expires_at", "expiresAt")):
            parsed = _expiry_datetime(value)
            if parsed:
                res.credential_expires_at = parsed
                return
    for key in ("credential", "credentials", "auth"):
        nested = event.get(key)
        if isinstance(nested, dict):
            _credential_expiry(nested, res, True)
            if res.credential_expires_at:
                return


class Adapter:
    provider = "base"

    def argv(self, spec: RunSpec) -> list[str]:
        raise NotImplementedError

    def env(self, spec: RunSpec) -> dict[str, str]:
        return {}

    def prepare(self, spec: RunSpec) -> None:
        """Write any config files the CLI needs for this run (inside the account HOME or the worktree)."""

    def parse_event(self, event: dict, res: RunResult) -> None:
        raise NotImplementedError

    # --- shared -----------------------------------------------------------------------------------------------
    def base_env(self, spec: RunSpec) -> dict[str, str]:
        env = {"HOME": spec.account_home, "USER": os.environ.get("USER", "aiflow-worker"), "LANG": "C.UTF-8",
               "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "TERM": "dumb", "NO_COLOR": "1"}
        env.update(scratch_env(spec))
        env.update(self.env(spec))
        env.update(spec.extra_env)
        return env

    def run(self, spec: RunSpec, on_progress: Callable[[dict], None] | None = None,
            stop: "threading.Event | None" = None) -> RunResult:
        self.prepare(spec)
        res = RunResult(log_path=spec.log_path)
        started = time.time()
        log = open(spec.log_path, "w", encoding="utf-8") if spec.log_path else None
        proc = subprocess.Popen(self.argv(spec), cwd=spec.cwd, env=self.base_env(spec), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        last_output = time.time()
        tail: list[str] = []
        buf = b""
        try:
            while True:
                if stop is not None and stop.is_set():
                    res.error_kind = "stopped"
                    break
                if time.time() - started > spec.timeout_s:
                    res.error_kind = "timeout"
                    break
                if time.time() - last_output > spec.idle_s:
                    res.error_kind = "idle"
                    break
                events = sel.select(timeout=1.0)
                if not events:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                last_output = time.time()
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace")
                    if log:
                        log.write(line + "\n")
                    tail.append(line)
                    tail = tail[-200:]
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        _credential_expiry(ev, res)
                        self.parse_event(ev, res)
                        if on_progress:
                            try:
                                on_progress(ev)
                            except Exception:  # noqa: BLE001
                                pass
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(15)
                except Exception:  # noqa: BLE001
                    os.killpg(proc.pid, signal.SIGKILL)
            res.exit_code = proc.returncode
            res.duration_s = time.time() - started
            if log:
                log.close()
        text_tail = "\n".join(tail[-60:])
        if not res.error_message and (res.error_kind or res.exit_code):
            for line in reversed(tail[-60:]):  # last non-JSON line or an error field from the stream
                try:
                    ev = json.loads(line)
                    msg = ev.get("message") or ev.get("error") if isinstance(ev, dict) else None
                    if isinstance(msg, dict):
                        msg = msg.get("message")
                    if msg:
                        res.error_message = str(msg)[:400]
                        break
                except ValueError:
                    if line.strip():
                        res.error_message = line.strip()[:400]
                        break
        if not res.error_kind:
            if QUOTA_PATTERN.search(text_tail) and (res.exit_code or not res.final_text):
                res.error_kind = "quota"
            elif AUTH_PATTERN.search(text_tail) and (res.exit_code or not res.final_text):
                res.error_kind = "auth"
            elif NETWORK_PATTERN.search(text_tail) and (res.exit_code or not res.final_text):
                res.error_kind = "network"
            elif res.exit_code not in (0, None) and not res.final_text:
                res.error_kind = "provider"
        m = None
        for m in RESULT_LINE.finditer(res.final_text or ""):
            pass
        if m:
            res.verdict, res.verdict_reason = m.group(2).upper(), m.group(3).strip()
        return res


def scratch_root(spec: RunSpec) -> Path:
    """Disk space for temp files and package caches of AI runs (/tmp on the lab machines is RAM)."""
    home = Path(spec.account_home)
    srv = home.parent.parent if home.parent.name == "accounts" else Path("/srv/aiflow")
    return srv / "cache" / "ai"


def scratch_env(spec: RunSpec) -> dict[str, str]:
    root = scratch_root(spec)
    for sub in ("tmp", "uv", "npm", "composer", "pip"):
        try:
            (root / sub).mkdir(parents=True, exist_ok=True)
            os.chmod(root / sub, 0o1777)  # shared by the AI user's runs; sticky like /tmp
        except OSError:
            pass
    if os.geteuid() == 0:
        # the worker runs as root, the AI as aiflow-worker: Codex's sandbox creates <root>/.git to protect it,
        # which fails ("bwrap: Can't mkdir") when the AI user does not own the writable root
        try:
            shutil.chown(root, "aiflow-worker", "aiflow-worker")
        except (LookupError, OSError):
            pass
    return {"TMPDIR": str(root / "tmp"), "UV_CACHE_DIR": str(root / "uv"), "npm_config_cache": str(root / "npm"),
            "COMPOSER_CACHE_DIR": str(root / "composer"), "PIP_CACHE_DIR": str(root / "pip")}


def get_adapter(provider: str) -> Adapter:
    if provider == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter()
    if provider == "codex":
        from .codex import CodexAdapter
        return CodexAdapter()
    if provider == "grok":
        from .grok import GrokAdapter
        return GrokAdapter()
    if provider == "opencode":
        from .opencode import OpenCodeAdapter
        return OpenCodeAdapter()
    raise ValueError(f"no adapter for provider {provider}")


def ts(value) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None
