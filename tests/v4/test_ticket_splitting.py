"""JAIF-360: shared split-plan contract used before any Jira child is created."""
from __future__ import annotations

import pytest

from aiflow.v4.core.ticket_splitting import (
    InvalidSplitPlan,
    MAX_CHILDREN,
    MIN_CHILDREN,
    validate_plan,
)


SECTIONS = "Files: aiflow/v4/x.py, tests/v4/test_x.py\nSkeleton: class X with run()\nTest cases: run returns the value"


def _child(n: int = 1, summary: str | None = None, description: str | None = None) -> dict[str, str]:
    return {
        "summary": summary if summary is not None else f"Child ticket {n} for the split",
        "description": description if description is not None else (
            f"Business requirement: do one thing.\nAC1: The child {n} change is testable.\n{SECTIONS}\n"
        ),
    }


def test_valid_two_item_plan_is_stripped_and_does_not_mutate_input():
    original = [
        {"summary": "  First child ticket  ", "description": "  AC1: First criterion.\n{}  ".format(SECTIONS)},
        _child(2),
    ]
    snapshot = [dict(item) for item in original]

    plan = validate_plan(original)

    assert plan == [
        {"summary": "First child ticket", "description": f"AC1: First criterion.\n{SECTIONS}"},
        {"summary": "Child ticket 2 for the split",
         "description": f"Business requirement: do one thing.\nAC1: The child 2 change is testable.\n{SECTIONS}"},
    ]
    assert original == snapshot
    assert plan is not original


def test_valid_eight_item_plan_passes():
    plan = validate_plan([_child(n) for n in range(1, MAX_CHILDREN + 1)])

    assert len(plan) == MAX_CHILDREN
    assert plan[0]["summary"].startswith("Child ticket 1")


@pytest.mark.parametrize(
    "value",
    [
        [_child(1)],
        [_child(n) for n in range(1, MAX_CHILDREN + 2)],
        "not a list",
        None,
        {"summary": "x", "description": "AC1: y"},
    ],
)
def test_list_bound_is_named_when_length_or_type_is_wrong(value):
    with pytest.raises(InvalidSplitPlan, match="between 2 and 8 tickets"):
        validate_plan(value)


def test_extra_keys_name_the_failing_index():
    item = {**_child(1), "labels": ["aiflow-ready"]}
    with pytest.raises(InvalidSplitPlan, match="split ticket 2 must contain only summary and description"):
        validate_plan([_child(1), item])


def test_empty_summary_names_the_failing_index():
    with pytest.raises(InvalidSplitPlan, match="split ticket 1 summary"):
        validate_plan([_child(1, summary="   "), _child(2)])


def test_missing_ac1_names_the_failing_index():
    with pytest.raises(InvalidSplitPlan, match="split ticket 2 needs an AC1"):
        validate_plan([_child(1), _child(2, description="No criterion here.")])


@pytest.mark.parametrize(
    "description",
    [
        f"AC1: Plain heading.\nMore text.\n{SECTIONS}",
        f"- AC1: List form.\n{SECTIONS}\n",
        f"* AC1: Star list form.\n{SECTIONS}",
        f"Intro\n  - AC1: Indented list form.\n{SECTIONS}\n",
    ],
)
def test_accepted_ac1_line_prefixes(description):
    plan = validate_plan([_child(1, description=description), _child(2)])

    assert plan[0]["description"] == description.strip()


def test_min_and_max_constants_match_the_ticket():
    assert (MIN_CHILDREN, MAX_CHILDREN) == (2, 8)


@pytest.mark.parametrize("missing", ["Files", "Skeleton", "Test cases"])
def test_a_child_without_a_required_section_is_rejected_and_named(missing):
    lines = [line for line in SECTIONS.split("\n") if not line.startswith(missing + ":")]
    description = "AC1: The change is testable.\n" + "\n".join(lines)
    with pytest.raises(InvalidSplitPlan, match=rf"split ticket 2 needs these sections with content: {missing}"):
        validate_plan([_child(1), _child(2, description=description)])


def test_an_empty_section_counts_as_missing():
    description = "AC1: The change is testable.\nFiles:\nSkeleton: class X\nTest cases: it runs"
    with pytest.raises(InvalidSplitPlan, match="needs these sections with content: Files"):
        validate_plan([_child(1), _child(2, description=description)])


def test_sections_may_be_bulleted_or_bold_and_hold_several_lines():
    description = ("AC1: The change is testable.\n- **Files:** aiflow/v4/x.py\n  tests/v4/test_x.py\n"
                   "* Skeleton: class X\n  def run(self)\nTest cases:\n- run returns the value\n- run rejects empty input")
    assert validate_plan([_child(1), _child(2, description=description)])[1]["description"] == description
