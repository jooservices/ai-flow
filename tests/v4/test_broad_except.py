"""JAIF-327: explanation parsing and shrink-only broad-except baseline rules."""
from __future__ import annotations

from pathlib import Path

from aiflow.v4.core import broad_except

ROOT = Path(__file__).resolve().parents[2]


def test_reason_on_handler_line_explains_broad_handler():
    source = "try:\n    run()\nexcept Exception:  # noqa: BLE001 - a local search failure is optional\n    pass\n"

    assert broad_except.unexplained_count(source) == 0


def test_reason_on_line_above_explains_broad_handler():
    source = "try:\n    run()\n# Expected: the remote store may be unavailable\nexcept BaseException:\n    pass\n"

    assert broad_except.unexplained_count(source) == 0


def test_reason_on_first_body_line_explains_broad_handler():
    source = "try:\n    run()\nexcept Exception:\n    pass  # : keep the optional lookup from blocking claims\n"

    assert broad_except.unexplained_count(source) == 0


def test_bare_noqa_does_not_explain_broad_handler():
    source = "try:\n    run()\nexcept Exception:  # noqa: BLE001\n    pass\n"

    assert broad_except.unexplained_count(source) == 1


def test_bare_pylint_disable_does_not_explain_broad_handler():
    source = "try:\n    run()\nexcept Exception:  # pylint: disable=broad-except\n    pass\n"

    assert broad_except.unexplained_count(source) == 1


def test_narrow_exception_tuple_is_not_a_broad_handler():
    source = "try:\n    run()\nexcept (OSError, ValueError):\n    pass\n"

    assert broad_except.unexplained_count(source) == 0


def test_bare_except_is_a_broad_handler():
    source = "try:\n    run()\nexcept:\n    pass\n"

    assert broad_except.unexplained_count(source) == 1


def test_nested_broad_handlers_count_separately():
    source = "try:\n    run()\nexcept Exception:\n    try:\n        retry()\n    except BaseException:\n        pass\n"

    assert broad_except.unexplained_count(source) == 2


def test_increased_file_count_fails():
    problems, reduced = broad_except.evaluate({"aiflow/v4/example.py": 2}, {"aiflow/v4/example.py": 1})

    assert problems == ["aiflow/v4/example.py: 2 unexplained broad handler(s), baseline allows 1"]
    assert reduced == []


def test_lowered_file_count_is_reported():
    problems, reduced = broad_except.evaluate({"aiflow/v4/example.py": 1}, {"aiflow/v4/example.py": 2})

    assert problems == []
    assert reduced == [("aiflow/v4/example.py", 2, 1)]


def test_new_baseline_entry_is_detected_against_base():
    assert broad_except.baseline_growth({"aiflow/v4/new.py": 1}, {}) == [
        "baseline aiflow/v4/new.py rises from 0 to 1"
    ]


def test_ci_runs_broad_except_guard_against_pull_request_base():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "broad-except:" in workflow
    job = workflow.split("  broad-except:\n", 1)[1].split("\n  quality:\n", 1)[0]
    assert "github.event_name == 'pull_request'" in job
    assert "github.event.pull_request.base.sha" in job
    assert "aiflow.v4.core.broad_except check" in job
