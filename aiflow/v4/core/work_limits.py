"""Shared developer work-time budgets (JAIF-371 defaults and JAIF-417 timeout growth).

A development ticket must be small enough for one short AI run: 15 minutes normally, a warning at 30 and a hard stop at 45. A
project can override the three values with ``limits: {work_minutes: {...}}`` in its control-repo file. ``SIZE_MINUTES`` is the
nominal developer time of each size label; a ticket above the target is split before a developer is offered it.
"""
from __future__ import annotations

from typing import Any

TARGET = 15
WARN = 30
HARD = 45
DEFAULTS: dict[str, int] = {"target": TARGET, "warn": WARN, "hard": HARD}
SIZE_MINUTES: dict[str, int] = {"XS": 5, "S": 15, "M": 30, "L": 60, "XL": 120}
TIMEOUT_RETRY_INCREMENT_MINUTES = 30
FULL_SUITE_TIME_SLICE_MINUTES = 15


def from_setting(setting: Any) -> dict[str, int]:
    """The three limits of a project setting, with the defaults for every missing value."""
    setting = setting if isinstance(setting, dict) else {}
    return {name: int(setting.get(name, default)) for name, default in DEFAULTS.items()}


def retry_hard_minutes(base_minutes: int, previous_timeouts: int) -> int:
    """Grow the next developer run's hard limit after each recorded timeout."""
    if isinstance(base_minutes, bool) or not isinstance(base_minutes, int) or base_minutes < 1:
        raise ValueError("base_minutes must be a positive integer")
    if isinstance(previous_timeouts, bool) or not isinstance(previous_timeouts, int) or previous_timeouts < 0:
        raise ValueError("previous_timeouts must be a non-negative integer")
    return base_minutes + previous_timeouts * TIMEOUT_RETRY_INCREMENT_MINUTES


def run_outcome_for_error(error_kind: str) -> str:
    """Store adapter timeouts as outcome=timeout so the next claim can count them."""
    return "timeout" if error_kind == "timeout" else "failed"
