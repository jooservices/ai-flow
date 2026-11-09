"""Study cases of known review defects: store, relevance, token cap, ledger and repeats (JAIF-584)."""
from __future__ import annotations

import json

from aiflow.v4.core import study_cases


def _case(case_id: str, areas: list[str] | None = None, task_types: list[str] | None = None, cause: str = "A cause.",
          signature: tuple[str, ...] = ("needle",), defect: str = "A defect.") -> dict:
    return {"id": case_id, "title": f"Title {case_id}", "areas": areas or [], "task_types": task_types or [],
            "defect": defect, "cause": cause, "fix": "Do the fix.", "signature": list(signature)}


def test_store_loads_every_seeded_case_with_a_cause_and_a_signature():
    cases = study_cases.load_cases()
    assert len(cases) == len(json.loads(study_cases.STORE.read_text(encoding="utf-8")))
    assert all(case["cause"] and case["signature"] for case in cases)
    assert len({case["id"] for case in cases}) == len(cases)


def test_load_cases_leaves_out_a_case_without_a_cause(tmp_path):
    store = tmp_path / "cases.json"
    store.write_text(json.dumps([_case("kept"), _case("observation", cause="")]), encoding="utf-8")
    assert [case["id"] for case in study_cases.load_cases(store)] == ["kept"]


def test_task_type_is_bug_only_for_a_bug_issue_type():
    assert study_cases.task_type_of("Bug") == "bug"
    assert study_cases.task_type_of(" bug ") == "bug"
    assert study_cases.task_type_of("Story") == "change"
    assert study_cases.task_type_of(None) == "change"


def test_ticket_paths_keeps_repository_paths_once_in_order():
    text = "Edit aiflow/v4/worker/dev.py and tests/v4/test_x.py; see aiflow/v4/worker/dev.py again."
    assert study_cases.ticket_paths(text) == ["aiflow/v4/worker/dev.py", "tests/v4/test_x.py"]
    assert study_cases.ticket_paths("") == []


def test_a_case_with_areas_applies_only_when_a_ticket_path_touches_one():
    case = _case("area", areas=["aiflow/v4/control/"])
    assert study_cases.relevance(case, ["aiflow/v4/control/x.py"], "change") == 1
    assert study_cases.relevance(case, ["aiflow/v4/worker/x.py"], "change") == -1
    assert study_cases.relevance(_case("general"), ["aiflow/v4/worker/x.py"], "change") == 0


def test_a_case_for_another_task_type_never_applies():
    case = _case("bugs", task_types=["bug"])
    assert study_cases.relevance(case, [], "change") == -1
    assert study_cases.relevance(case, [], "bug") == 0


def test_selection_puts_the_most_specific_case_first_and_skips_irrelevant_ones():
    cases = [_case("general"), _case("two", areas=["aiflow/v4/control/", "docs/v4/"]),
             _case("one", areas=["aiflow/v4/control/"]), _case("other", areas=["deploy/v4/"])]
    chosen = study_cases.select_cases(cases, ["aiflow/v4/control/a.py", "docs/v4/b.md"], "change")
    assert [case["id"] for case in chosen] == ["two", "one", "general"]


def test_selection_never_exceeds_the_token_cap():
    cases = [_case(f"case-{index:02d}", defect="x" * 200) for index in range(20)]
    chosen = study_cases.select_cases(cases, [], "change", cap_tokens=150)
    block = study_cases.lessons_block(chosen)
    assert chosen and len(chosen) < len(cases)
    assert len(block) <= 150 * study_cases.CHARS_PER_TOKEN


def test_the_default_cap_holds_for_the_seeded_store():
    chosen = study_cases.select_cases(study_cases.load_cases(), ["aiflow/v4/control/x.py", "tests/v4/t.py"], "change")
    assert len(study_cases.lessons_block(chosen)) <= study_cases.TOKEN_CAP * study_cases.CHARS_PER_TOKEN


def test_no_applicable_case_gives_an_empty_section():
    assert study_cases.lessons_block([]) == ""


def test_a_repeated_defect_is_found_only_for_a_case_given_before_to_the_same_ticket():
    cases = [_case("seen", signature=("needle",)), _case("unseen", signature=("other",))]
    ledger = [{"ticket": "PRJ-1", "given": ["seen", "unseen"]}, {"ticket": "PRJ-2", "given": ["unseen"]}]
    assert study_cases.repeated_case_ids("PRJ-1", ledger, "The Needle is still here", cases) == ["seen"]
    assert study_cases.repeated_case_ids("PRJ-2", ledger, "needle", cases) == []
    assert study_cases.repeated_case_ids("PRJ-1", ledger, "", cases) == []


def test_deliver_lessons_records_the_run_in_the_ledger_and_returns_the_section(tmp_path):
    ticket = {"key": "PRJ-7", "type": "Bug", "summary": "Fix", "description": "Edit aiflow/v4/control/x.py"}
    cases = [_case("first", areas=["aiflow/v4/control/"], signature=("needle",))]

    block = study_cases.deliver_lessons(tmp_path, ticket, "run-1", "", cases)
    again = study_cases.deliver_lessons(tmp_path, ticket, "run-2", "the needle again", cases)

    assert "[first]" in block and block.startswith(study_cases.LESSON_HEADER)
    rows = study_cases.read_ledger(tmp_path)
    assert [(row["run_id"], row["given"], row["repeated"]) for row in rows] == [
        ("run-1", ["first"], []), ("run-2", ["first"], ["first"])]
    assert "[first]" in again


def test_deliver_lessons_gives_nothing_and_still_records_the_run(tmp_path):
    ticket = {"key": "PRJ-8", "type": "Story", "summary": "Plain", "description": ""}
    assert study_cases.deliver_lessons(tmp_path, ticket, "run-1", "", [_case("bugs", task_types=["bug"])]) == ""
    assert study_cases.read_ledger(tmp_path)[0]["given"] == []
