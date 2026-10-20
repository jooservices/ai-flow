"""Read-only quota levels of the AIs, and whether each one may be given work.

    bin/aiflow-quota [--json]

Codex writes its rate limits (5 hour and weekly `used_percent`, `resets_at`) into its own session logs: that is read here (verified
on 2026-10-20). Claude, Grok and OpenCode have no verified source yet and are reported as `unknown`; the failure-signal cooldown of the
supervisor stays their fallback. Minimum remaining quota per AI (owner decision 2026-10-20): Claude 20, Codex 5, Grok 10, OpenCode 3."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

MINIMUM = {"Claude": 20.0, "Codex": 5.0, "Grok": 10.0, "OpenCode": 3.0}
SESSIONS = Path.home() / ".codex" / "sessions"


def _find_limits(node: Any) -> dict | None:
    if isinstance(node, dict):
        limits = node.get("rate_limits")
        if isinstance(limits, dict) and limits.get("primary") and limits.get("limit_id") in (None, "codex"):
            return limits
        for value in node.values():
            found = _find_limits(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_limits(value)
            if found:
                return found
    return None


def _stamp(record: Any) -> float | None:
    value = record.get("timestamp") if isinstance(record, dict) else None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if isinstance(value, str) else None
    except ValueError:
        return None


def last_limits(lines: list[str], with_time: bool = False):
    """The newest `rate_limits` object in a Codex session log (and, with `with_time`, the time of its record)."""
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            record = json.loads(line)
            found = _find_limits(record)
        except ValueError:
            continue
        if found:
            return (found, _stamp(record)) if with_time else found
    return (None, None) if with_time else None


def windows(limits: dict, now: float) -> list[dict[str, Any]]:
    out = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, dict) or "used_percent" not in window:
            continue
        reset = window.get("resets_at")
        expired = bool(reset) and reset <= now
        used = 0.0 if expired else float(window["used_percent"])
        out.append({"window": f"{int(window.get('window_minutes') or 0)} min", "used_percent": used, "left_percent": round(100 - used, 1),
                    "resets_at": reset, "window_already_reset": expired})
    return out


def codex_level(sessions: Path = SESSIONS, now: float | None = None, files: int = 8) -> dict[str, Any]:
    now = time.time() if now is None else now
    logs = sorted(sessions.rglob("rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:files] if sessions.is_dir() else []
    best: tuple[float, dict] | None = None
    for log in logs:
        try:
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
            modified = log.stat().st_mtime
        except OSError:
            continue
        limits, stamp = last_limits(lines, with_time=True)
        if limits and (best is None or (stamp or modified) > best[0]):
            best = (stamp or modified, limits)
    if best:
        return {"known": True, "source": "codex session log", "observed_age_minutes": round((now - best[0]) / 60),
                "windows": windows(best[1], now)}
    return {"known": False, "source": "no Codex rate_limits found"}


def eligibility(ai: str, level: dict[str, Any], minimum: dict[str, float] = MINIMUM) -> dict[str, Any]:
    floor = minimum.get(ai, 5.0)
    if not level.get("known"):
        return {"ai": ai, "known": False, "minimum_left": floor, "eligible": None, "why": "quota level unknown: the cooldown fallback applies"}
    lowest = min((w["left_percent"] for w in level["windows"]), default=None)
    if lowest is None:
        return {"ai": ai, "known": False, "minimum_left": floor, "eligible": None, "why": "no window reported"}
    return {"ai": ai, "known": True, "minimum_left": floor, "lowest_left": lowest, "eligible": lowest >= floor,
            "why": "ok" if lowest >= floor else f"{lowest}% left in the tightest window, below {floor}%"}


def collect(now: float | None = None, sessions: Path = SESSIONS) -> list[dict[str, Any]]:
    levels = {"Codex": codex_level(sessions, now)}
    for ai in ("Claude", "Grok", "OpenCode"):
        levels[ai] = {"known": False, "source": "no verified source yet"}
    return [{**eligibility(ai, levels[ai]), "level": levels[ai]} for ai in ("Claude", "Codex", "Grok", "OpenCode")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only quota levels of the AIs.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = collect()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        state = {True: "eligible", False: "NOT eligible", None: "unknown"}[row["eligible"]]
        print(f"{row['ai']:<9} min {row['minimum_left']:>4g}%  {state:<12} {row['why']}")
        for window in row["level"].get("windows", []):
            reset = time.strftime("%m-%d %H:%M", time.localtime(window["resets_at"])) if window.get("resets_at") else "?"
            note = " (window already reset)" if window["window_already_reset"] else ""
            print(f"          window {window['window']:>9}: {window['left_percent']}% left, resets {reset}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
