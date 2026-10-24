"""Shared result contract for the machine and project readiness check.

The check reports only state and safe, human-readable guidance. Credentials are represented by present/valid
state; values, paths to credential files, and provider error text never belong in a condition result.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

ResultState = Literal["ready", "fixed_automatically", "needs_action", "not_possible_on_this_machine"]
RESULT_STATES: tuple[ResultState, ...] = (
    "ready", "fixed_automatically", "needs_action", "not_possible_on_this_machine",
)


@dataclass(frozen=True)
class Condition:
    id: str
    name: str
    result: ResultState
    detail: str
    action: str = ""
    affects_work: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def condition(condition_id: str, name: str, result: ResultState, detail: str,
              action: str = "", affects_work: bool = True) -> dict[str, object]:
    """Build a serializable condition with one of the four supported outcomes."""
    if result not in RESULT_STATES:
        raise ValueError(f"unsupported readiness result: {result}")
    return Condition(condition_id, name, result, detail, action, affects_work).as_dict()
