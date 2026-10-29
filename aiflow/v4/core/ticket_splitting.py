"""Provider-independent contract for an ordered intake split plan.

Intake and control share ``validate_plan`` so a later ticket can create Jira children
from a list that is already known to be usable. This module does not call Jira,
HTTP, a database, an estimator or a worker.
"""
from __future__ import annotations

from typing import Any

MIN_CHILDREN = 2
MAX_CHILDREN = 8
SUMMARY_MIN = 5
SUMMARY_MAX = 240
DESCRIPTION_MAX = 10_000
_AC1_PREFIXES = ("AC1:", "- AC1:", "* AC1:")


class InvalidSplitPlan(ValueError):
    """A split plan is missing a usable, testable child ticket."""


def _has_ac1(description: str) -> bool:
    return any(line.strip().startswith(_AC1_PREFIXES) for line in description.splitlines())


def validate_plan(value: Any) -> list[dict[str, str]]:
    """Return a new list of stripped child tickets, or raise ``InvalidSplitPlan``."""
    if not isinstance(value, list) or not MIN_CHILDREN <= len(value) <= MAX_CHILDREN:
        raise InvalidSplitPlan(
            f"split plan must contain between {MIN_CHILDREN} and {MAX_CHILDREN} tickets"
        )
    plan: list[dict[str, str]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) != {"summary", "description"}:
            raise InvalidSplitPlan(
                f"split ticket {index} must contain only summary and description"
            )
        summary, description = item["summary"], item["description"]
        if not isinstance(summary, str) or not SUMMARY_MIN <= len(summary.strip()) <= SUMMARY_MAX:
            raise InvalidSplitPlan(
                f"split ticket {index} summary must be {SUMMARY_MIN} to {SUMMARY_MAX} characters"
            )
        if (
            not isinstance(description, str)
            or not 1 <= len(description.strip()) <= DESCRIPTION_MAX
        ):
            raise InvalidSplitPlan(
                f"split ticket {index} description must be 1 to {DESCRIPTION_MAX} characters"
            )
        if not _has_ac1(description):
            raise InvalidSplitPlan(f"split ticket {index} needs an AC1 acceptance criterion")
        plan.append({"summary": summary.strip(), "description": description.strip()})
    return plan
