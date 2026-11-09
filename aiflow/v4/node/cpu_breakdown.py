"""CPU use of a node split into AI tools, tests, browsers and databases (JAIF-595 / JAIF-337 AC6).

Every heartbeat carries `metrics.cpu_breakdown`: for each group, the share of the whole machine's CPU that its
processes used during a short sample, in percent (0 to 100, the same scale as `cpu_pct`). The Dashboard shows it for
each node so an operator can see whether the AI tools or the tests they start are what loads a machine. A process
belongs to a group by its name; interpreters (python, node, php, uv) are told apart by the command they run.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

GROUPS = ("ai_tools", "tests", "browsers", "databases")
SAMPLE_S = 0.25
AI_TOOLS = frozenset({"claude", "codex", "opencode", "gemini"})
BROWSERS = ("chrome", "chromium", "headless_shell", "firefox", "msedge", "webkit", "playwright")
DATABASES = ("postgres", "mariadbd", "mysqld", "redis-server", "valkey-server", "mongod")
TEST_COMMANDS = ("pytest", "phpunit", "unittest", "vitest", "jest", "playwright test", "go test", "cargo test")
INTERPRETERS = frozenset({
    "python", "python3", "node", "php", "uv", "npm", "npx", "composer", "go", "cargo", "bash", "sh",
})


def is_interpreter(name: str) -> bool:
    return name in INTERPRETERS or name.startswith("python")


def classify(name: str, command: str = "") -> str | None:
    """The group of a process, from its name and (for interpreters) its command line; None for everything else."""
    lowered = name.lower()
    if lowered in AI_TOOLS:
        return "ai_tools"
    if lowered.startswith(DATABASES):
        return "databases"
    if lowered.startswith(BROWSERS):
        return "browsers"
    if lowered in ("pytest", "phpunit"):
        return "tests"
    return classify_command(command.lower()) if is_interpreter(lowered) else None


def classify_command(command: str) -> str | None:
    """An interpreter runs an AI tool (its script is one of the first two words) or a test runner (any word)."""
    words = command.split()
    if any(Path(word).name in AI_TOOLS for word in words[:2]):
        return "ai_tools"
    return "tests" if any(marker in command for marker in TEST_COMMANDS) else None


def _read_process(proc: Path) -> tuple[str, int] | None:
    """The name and the CPU jiffies (user and system) of one process, or None when it ended meanwhile."""
    try:
        stat = (proc / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    opening, closing = stat.find("("), stat.rfind(")")
    if opening < 0 or closing < opening:
        return None
    fields = stat[closing + 2:].split()
    try:
        return stat[opening + 1:closing], int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def _command(proc: Path) -> str:
    try:
        return (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except OSError:
        return ""


def sample_jiffies(proc_root: Path = Path("/proc")) -> dict[str, int]:
    """Total CPU jiffies of the live processes of each group."""
    totals = {group: 0 for group in GROUPS}
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        process = _read_process(proc)
        if process is None:
            continue
        name, jiffies = process
        group = classify(name, _command(proc) if is_interpreter(name.lower()) else "")
        if group:
            totals[group] += jiffies
    return totals


def shares(before: dict[str, int], after: dict[str, int], seconds: float, cpus: int, ticks: int) -> dict[str, float]:
    """Percent of the whole machine each group used between two samples (a process that ended counts as zero)."""
    capacity = max(seconds, 1e-6) * max(cpus, 1) * max(ticks, 1)
    return {
        group: round(min(100.0, max(0, after[group] - before[group]) * 100.0 / capacity), 1)
        for group in GROUPS
    }


def measure(proc_root: Path = Path("/proc"), sample_s: float = SAMPLE_S,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, float]:
    """Sample the groups twice, `sample_s` apart, and return each group's share of the machine."""
    before, started = sample_jiffies(proc_root), time.monotonic()
    sleep(sample_s)
    after = sample_jiffies(proc_root)
    return shares(before, after, time.monotonic() - started, os.cpu_count() or 1, os.sysconf("SC_CLK_TCK"))
