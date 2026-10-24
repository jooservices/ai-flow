"""JAIF-273: report the develop giant-file baseline and prevent regressions or new giants."""
from __future__ import annotations

from pathlib import Path

from aiflow.v4.core import giant_files

ROOT = Path(__file__).resolve().parents[2]


def test_a_longer_baseline_giant_fails():
    baseline = {"aiflow/v4/control/api.py": 1_501}
    head = {"aiflow/v4/control/api.py": 1_502}

    giants, failures = giant_files.evaluate(baseline, set(baseline), head)

    assert giants == baseline
    assert failures == [("grown", "aiflow/v4/control/api.py", 1_501, 1_502)]


def test_a_shorter_baseline_giant_passes():
    baseline = {"aiflow/v4/control/ui/console.html": 5_870}
    head = {"aiflow/v4/control/ui/console.html": 5_869}

    giants, failures = giant_files.evaluate(baseline, set(baseline), head)

    assert giants == baseline
    assert failures == []


def test_a_new_file_at_the_limit_passes():
    giants, failures = giant_files.evaluate({}, {"README.md"}, {"aiflow/v4/core/new_feature.py": 1_500})

    assert giants == {}
    assert failures == []


def test_a_new_file_over_the_limit_fails():
    path = "aiflow/v4/core/new_feature.py"

    giants, failures = giant_files.evaluate({}, {"README.md"}, {path: 1_501})

    assert giants == {}
    assert failures == [("new", path, 0, 1_501)]


def test_failure_message_directs_developer_ai_to_a_new_module(monkeypatch, capsys):
    monkeypatch.setattr(
        giant_files,
        "check",
        lambda _base, _head: ({"aiflow/v4/control/api.py": 7_546},
                              [("grown", "aiflow/v4/control/api.py", 7_546, 7_547)]),
    )

    assert giant_files.main(["--base", "origin/develop"]) == 1
    output = capsys.readouterr().out
    assert "aiflow/v4/control/api.py: 7,546 lines" in output
    assert "Developer AI: put new code for the feature in a new module" in output


def test_ci_runs_the_check_on_every_pull_request_against_develop():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "giant-files:" in workflow
    job = workflow.split("  giant-files:\n", 1)[1].split("\n  v4:\n", 1)[0]
    assert "github.event_name == 'pull_request'" in job
    assert "github.event.pull_request.base.sha" in job
    assert "origin/develop" in job
    assert "aiflow.v4.core.giant_files" in job
