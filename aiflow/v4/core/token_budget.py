"""Per-task token budgets by size (JAIF-585), phase one: measure only.

A task is one ticket. Its tokens are the completed development and verification tokens, summed the same way as
``capacity.history``. The four budget groups cover the five ticket sizes: XS and S are small, M medium, L large and XL
huge. Phase one only lists the tasks above their budget; nothing stops or reassigns work. Phase two enforces the budget
after the owner has reviewed the numbers. This module is pure: callers pass plain sample mappings with ``ticket``,
``size`` and ``tokens`` keys, so core never imports control.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SIZE_GROUPS: dict[str, str] = {"XS": "small", "S": "small", "M": "medium", "L": "large", "XL": "huge"}
# Provisional: the upper token bound of each size's starting range in capacity.SIZE_BASELINES, until the measured
# history replay sets the owner's numbers.
BUDGET_TOKENS: dict[str, int] = {"small": 16_000, "medium": 32_000, "large": 64_000, "huge": 128_000}


def budget_group(size: str) -> str:
    """Return the budget group of a ticket size; an unknown size is refused so no task goes unchecked."""
    try:
        return SIZE_GROUPS[size]
    except KeyError:
        raise ValueError(f"unknown ticket size: {size!r}") from None


def find_over_budget(samples: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Count the checked tasks and list each one whose tokens exceed its group's budget, largest overrun first."""
    checked = 0
    over: list[dict[str, Any]] = []
    for sample in samples:
        checked += 1
        group = budget_group(str(sample["size"]))
        budget = BUDGET_TOKENS[group]
        tokens = int(sample["tokens"])
        if tokens > budget:
            over.append({"ticket": str(sample["ticket"]), "size": sample["size"], "group": group,
                         "tokens": tokens, "budget": budget, "over_by": tokens - budget})
    over.sort(key=lambda row: row["over_by"], reverse=True)
    return {"checked": checked, "over_budget_count": len(over), "over_budget": over}
