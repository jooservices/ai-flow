"""Per-task token budgets by size: the four groups and the replay that lists tasks above budget (JAIF-585)."""
from __future__ import annotations

import pytest

from aiflow.v4.control import capacity
from aiflow.v4.core import token_budget


def sample(ticket, size, tokens):
    return {"ticket": ticket, "size": size, "tokens": tokens}


def test_every_capacity_size_maps_to_one_of_the_four_budget_groups():
    assert set(token_budget.SIZE_GROUPS) == set(capacity.SIZES)
    assert set(token_budget.SIZE_GROUPS.values()) == set(token_budget.BUDGET_TOKENS)


def test_four_groups_have_budgets_that_grow_with_size():
    assert list(token_budget.BUDGET_TOKENS) == ["small", "medium", "large", "huge"]
    budgets = list(token_budget.BUDGET_TOKENS.values())
    assert budgets == sorted(set(budgets))


@pytest.mark.parametrize("size,group", [("XS", "small"), ("S", "small"), ("M", "medium"), ("L", "large"), ("XL", "huge")])
def test_budget_group_of_each_size(size, group):
    assert token_budget.budget_group(size) == group


def test_an_unknown_size_is_refused_instead_of_skipped():
    with pytest.raises(ValueError, match="unknown ticket size"):
        token_budget.budget_group("XXL")


def test_a_task_exactly_at_its_budget_is_not_over():
    budget = token_budget.BUDGET_TOKENS["small"]
    result = token_budget.find_over_budget([sample("K-1", "XS", budget), sample("K-2", "S", budget + 1)])
    assert result["checked"] == 2
    assert [row["ticket"] for row in result["over_budget"]] == ["K-2"]
    assert result["over_budget"][0] == {"ticket": "K-2", "size": "S", "group": "small",
                                        "tokens": budget + 1, "budget": budget, "over_by": 1}


def test_replay_lists_every_task_above_its_budget_largest_overrun_first():
    history = [sample("K-1", "M", 40_000), sample("K-2", "L", 70_000), sample("K-3", "XL", 100_000),
               sample("K-4", "M", 10_000), sample("K-5", "XL", 200_000)]
    result = token_budget.find_over_budget(history)
    assert result["checked"] == 5
    assert result["over_budget_count"] == 3
    assert [row["ticket"] for row in result["over_budget"]] == ["K-5", "K-1", "K-2"]


def test_empty_history_checks_nothing():
    assert token_budget.find_over_budget([]) == {"checked": 0, "over_budget_count": 0, "over_budget": []}
