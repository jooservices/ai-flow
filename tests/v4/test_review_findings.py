"""Pure finding-history rules for AI verification rounds."""
from __future__ import annotations

from aiflow.v4.core import review_findings


def test_section_reads_only_the_requested_bullet_section():
    text = "REVIEW FINDINGS:\n- F1 [FIXED]\nCHECKS: tests\n- unrelated"
    assert review_findings._section(text, "REVIEW FINDINGS") == "- F1 [FIXED]\n"
    assert review_findings._section(text, "MISSING") is None


def test_legacy_findings_keep_wording_without_list_number_or_id():
    assert review_findings.legacy_findings("FINDINGS:\n- 1. F1: exact old wording\n* another issue\n") == [
        "exact old wording", "another issue"]


def test_findings_are_numbered_for_the_developer_and_exclude_fixed_items():
    history = [{"id": "F1", "wording": "fixed", "status": "fixed"},
               {"id": "F2", "wording": "retry fails", "status": "not_fixed"}]
    assert review_findings.format_findings(history) == "- 1. F2: retry fails"


def test_history_render_includes_wording_and_latest_status():
    history = [{"id": "F1", "wording": "retry fails", "status": "not_fixed"}]
    assert review_findings.format_review_history(history) == "- F1 [NOT FIXED]: retry fails"
    assert review_findings.format_review_history([]) == "No earlier review findings."


def test_legacy_status_helper_keeps_earlier_ids_open_and_returns_only_new_text():
    history = [{"id": "F1", "wording": "retry fails", "status": "new"}]

    new_items = review_findings._legacy_new_items("FINDINGS:\n- F1: retry fails\n- empty input fails\n", history)

    assert history[0]["status"] == "not_fixed"
    assert new_items == [("", "empty input fails")]


def test_structured_status_helper_reads_fixed_unresolved_and_new_findings():
    states, new_items = review_findings._structured_findings(
        "- F1 [FIXED]\n- F2 [NOT FIXED]: repeat the earlier wording\n- NEW: empty input fails\n")

    assert states == {"F1": "fixed", "F2": "not_fixed"}
    assert new_items == [("", "empty input fails")]


def test_new_finding_helper_skips_requested_ids_already_in_history():
    history = [{"id": "F1", "wording": "existing", "status": "not_fixed", "first_round": 1}]

    review_findings._append_new_findings(history, [("F2", "requested"), ("", "generated")], 3)

    assert [item["id"] for item in history] == ["F1", "F2", "F3"]
    assert history[-1]["first_round"] == 3


def test_unresolved_findings_repeat_the_first_review_wording():
    previous = [{"id": "F1", "wording": "retry fails when the token expires", "status": "new",
                 "first_round": 1}]
    text = "REVIEW FINDINGS:\n- F1 [NOT FIXED]: retry still fails\n- NEW: empty input is accepted\n"

    history, active, new_count = review_findings.update_review_findings(text, previous, 2)

    assert history == [
        {"id": "F1", "wording": "retry fails when the token expires", "status": "not_fixed", "first_round": 1},
        {"id": "F2", "wording": "empty input is accepted", "status": "new", "first_round": 2},
    ]
    assert active == "- 1. F1: retry fails when the token expires\n- 2. F2: empty input is accepted"
    assert new_count == 1


def test_missing_review_status_does_not_silently_close_an_earlier_finding():
    previous = [{"id": "F1", "wording": "retry still fails", "status": "not_fixed", "first_round": 1}]

    history, active, _count = review_findings.update_review_findings("VERDICT: PASS\n", previous, 2)

    assert history[0]["status"] == "not_fixed"
    assert active == "- 1. F1: retry still fails"


def test_legacy_review_history_is_recovered_from_rework_runs():
    runs = [
        {"outcome": "rework", "details": {"verdict": "FAIL", "findings": "- retry is broken"}},
        {"outcome": "rework", "details": {"verdict": "FAIL", "findings": "- retry is broken\n- empty input fails"}},
    ]

    assert review_findings.history_from_runs(runs) == [
        {"id": "F1", "wording": "retry is broken", "status": "not_fixed", "first_round": 1},
        {"id": "F2", "wording": "empty input fails", "status": "new", "first_round": 2},
    ]


def test_stored_history_ignores_blocked_runs_without_a_completed_review():
    blocked = {"details": {"verdict": "BLOCKED", "review_findings": []}}
    passed = {"details": {"verdict": "PASS", "review_findings": [
        {"id": "F1", "wording": "fixed", "status": "fixed"}]}}

    assert review_findings._stored_history(blocked) is None
    assert review_findings._stored_history(passed) == passed["details"]["review_findings"]


def test_legacy_history_rebuilds_each_returned_finding():
    runs = [{"outcome": "rework", "details": {"verdict": "FAIL", "findings": "- retry fails"}}]

    assert review_findings._legacy_history(runs)[0]["wording"] == "retry fails"


def test_round_count_skips_non_review_runs():
    runs = [
        {"account_id": "offline", "outcome": "failed", "details": {}},
        {"account_id": "reviewer-one", "outcome": "rework", "details": {"verdict": "FAIL"}},
        {"account_id": "reviewer-two", "outcome": "merged", "details": {"verdict": "PASS", "review_findings": []}},
    ]

    assert review_findings.review_rounds_completed(runs) == 2
