"""Resolve the work-time limits recorded for an individual run."""
from __future__ import annotations


DEFAULTS = {"target": 30, "warn": 45, "hard": 60}


def effective_work_minutes(project, details: dict | None = None) -> dict[str, int]:
    """Return project limits with a valid per-run override, such as a timeout retry's larger hard limit."""
    limits = project.work_minutes() if project else dict(DEFAULTS)
    recorded = (details or {}).get("effective_work_minutes")
    if not isinstance(recorded, dict):
        return limits
    for name in DEFAULTS:
        value = recorded.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            limits[name] = max(limits[name], value) if name == "hard" else value
    return limits
