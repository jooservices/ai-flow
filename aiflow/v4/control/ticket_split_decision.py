"""Decide whether a ticket needs splitting from the existing capacity estimate."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import capacity
from ..core.ticket_splitting import DEFAULT_TARGET_MINUTES, estimate_needs_split, validate_plan

EstimateFn = Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], dict[str, Any]]


def decide_split(
    ticket: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    *,
    target_minutes: int = DEFAULT_TARGET_MINUTES,
    plan: Any = None,
    estimate: EstimateFn = capacity.estimate,
) -> dict[str, Any]:
    """Return the estimate and ``needs_split``. Does not call Jira or mutate ``ticket``."""
    if plan is not None:
        validate_plan(plan)
    estimated = estimate(dict(ticket), list(samples))
    return {"estimate": estimated, "needs_split": estimate_needs_split(estimated, target_minutes)}
