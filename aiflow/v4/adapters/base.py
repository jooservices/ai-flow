"""Adapters turn a run (prompt, worktree, account, model) into one AI CLI process and read its result.

All CLIs are run without a terminal, with the account's own HOME and config directory, and with output in
their JSON event format. The run never commits or pushes: the worker does that after the AI exits.
"""
from __future__ import annotations

import json
import os
import re
import selectors
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
    error_kind: str = ""       # "" | quota | auth | provider | timeout | idle | other
    reset_at: datetime | None = None
    quota: dict = field(default_factory=dict)
    duration_s: float = 0.0
    log_path: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["reset_at"] = self.reset_at.isoformat() if self.reset_at else None
        return d


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
        if not res.error_kind:
            if QUOTA_PATTERN.search(text_tail) and (res.exit_code or not res.final_text):
                res.error_kind = "quota"
            elif AUTH_PATTERN.search(text_tail) and (res.exit_code or not res.final_text):
                res.error_kind = "auth"
            elif res.exit_code not in (0, None) and not res.final_text:
                res.error_kind = "provider"
        m = None
        for m in RESULT_LINE.finditer(res.final_text or ""):
            pass
        if m:
            res.verdict, res.verdict_reason = m.group(2).upper(), m.group(3).strip()
        return res


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
