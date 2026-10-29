"""Provider-independent contract for an ordered intake split plan.

Intake and control share ``validate_plan`` so a later ticket can create Jira children
from a list that is already known to be usable. ``estimate_needs_split`` compares an
existing estimate's high minutes with the project target. This module does not call
Jira, HTTP, a database, an estimator or a worker.
"""
from __future__ import annotations

import re
from typing import Any

MIN_CHILDREN = 2
MAX_CHILDREN = 8
SUMMARY_MIN = 5
SUMMARY_MAX = 240
DESCRIPTION_MAX = 10_000
DEFAULT_TARGET_MINUTES = 30
_AC1_PREFIXES = ("AC1:", "- AC1:", "* AC1:")
# What the Team Lead gives the developer in every child: where to work, the shape of the code, the tests to write.
REQUIRED_SECTIONS = ("Files", "Skeleton", "Test cases")


class InvalidSplitPlan(ValueError):
    """A split plan is missing a usable, testable child ticket."""


def _has_ac1(description: str) -> bool:
    return any(line.strip().startswith(_AC1_PREFIXES) for line in description.splitlines())


def section_gaps(description: str) -> list[str]:
    """Names of the required sections (``Files:``, ``Skeleton:``, ``Test cases:``) that are missing or empty."""
    gaps = []
    for name in REQUIRED_SECTIONS:
        match = re.search(rf"^\s*(?:[-*]\s*)?\**{re.escape(name)}\**\s*:\s*(.*?)(?=^\s*(?:[-*]\s*)?\**(?:"
                          + "|".join(re.escape(n) for n in ("AC1", *REQUIRED_SECTIONS)) + r")\**\s*:|\Z)",
                          description, re.I | re.M | re.S)
        if not match or not match.group(1).strip():
            gaps.append(name)
    return gaps


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
        if gaps := section_gaps(description):
            raise InvalidSplitPlan(f"split ticket {index} needs these sections with content: " + ", ".join(gaps))
        plan.append({"summary": summary.strip(), "description": description.strip()})
    return plan


def estimate_needs_split(estimate: Any, target_minutes: int = DEFAULT_TARGET_MINUTES) -> bool:
    """Return True when the estimate's high minutes exceed the project target."""
    if isinstance(target_minutes, bool) or not isinstance(target_minutes, int) or target_minutes < 1:
        raise ValueError("target_minutes must be a positive integer")
    minutes = estimate.get("time_minutes") if isinstance(estimate, dict) else None
    if not isinstance(minutes, dict) or minutes.get("high") is None:
        raise ValueError("estimate must include time_minutes.high")
    return float(minutes["high"]) > target_minutes
