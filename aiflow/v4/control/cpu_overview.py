"""CPU use of each machine split into AI tools, tests, browsers and databases (JAIF-595), read from node heartbeats.

The overview shows machine names and percentages only. It is served without a sign-in so an operator can see at a
glance what loads the machines (docs/v4/dashboard-cpu.md).
"""
from __future__ import annotations

import math

GROUPS = ("ai_tools", "tests", "browsers", "databases")
STALE_AFTER_S = 75  # three missed heartbeats, the same limit that marks a machine offline


def _percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(min(max(float(value), 0.0), 100.0), 1)


def machine_cpu(row: dict) -> dict:
    """One machine: its whole CPU use and the share of each group; None where the agent has not reported yet."""
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    reported = metrics.get("cpu_breakdown") if isinstance(metrics.get("cpu_breakdown"), dict) else {}
    age = row.get("age")
    return {
        "node": row["name"],
        "role": row.get("role") or "",
        "online": age is not None and float(age) <= STALE_AFTER_S,
        "cpus": metrics.get("cpus") if isinstance(metrics.get("cpus"), int) else None,
        "cpu_pct": _percent(metrics.get("cpu_pct")),
        "breakdown": {group: _percent(reported.get(group)) for group in GROUPS},
    }


def overview(rows: list[dict]) -> dict:
    return {"nodes": [machine_cpu(row) for row in rows]}
