"""Provider-independent contract for ordered intake split plans."""
from __future__ import annotations

from typing import Any

from .work_limits import TARGET_WORK_MINUTES

TARGET_MINUTES = TARGET_WORK_MINUTES
MAX_CHILDREN = 8


class InvalidSplitPlan(ValueError):
    """A split plan is missing a usable, testable child ticket."""


def validate_plan(value: Any) -> list[dict[str, str]]:
    """Validate the bounded ticket list shared by the intake worker and control service."""
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_CHILDREN:
        raise InvalidSplitPlan(f"split plan must contain between 2 and {MAX_CHILDREN} tickets")
    plan = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) != {"summary", "description"}:
            raise InvalidSplitPlan(f"split ticket {index} must contain only summary and description")
        summary, description = item["summary"], item["description"]
        if not isinstance(summary, str) or not 5 <= len(summary.strip()) <= 240:
            raise InvalidSplitPlan(f"split ticket {index} summary must be 5 to 240 characters")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 10_000:
            raise InvalidSplitPlan(f"split ticket {index} description must be 1 to 10000 characters")
        if not any(line.strip().startswith(("AC1:", "- AC1:", "* AC1:")) for line in description.splitlines()):
            raise InvalidSplitPlan(f"split ticket {index} needs an AC1 acceptance criterion")
        plan.append({"summary": summary.strip(), "description": description.strip()})
    return plan
