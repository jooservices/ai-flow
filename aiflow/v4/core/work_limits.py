"""Shared developer work-time budgets."""
from __future__ import annotations

TIMEOUT_RETRY_INCREMENT_MINUTES = 30


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
