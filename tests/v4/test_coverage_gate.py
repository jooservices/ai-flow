"""Coverage job, Codecov upload and README badges (JAIF-574)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from aiflow.v4.core import pipeline_checks


ROOT = Path(__file__).resolve().parents[2]
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
CI = yaml.safe_load(CI_TEXT)
README = (ROOT / "README.md").read_text(encoding="utf-8")
DOCS = (ROOT / "docs/v4/coverage-gate.md").read_text(encoding="utf-8")
DEPLOY_README = (ROOT / "deploy/v4/README.md").read_text(encoding="utf-8")


def test_coverage_job_is_parallel_path_filtered_and_under_seven_minutes():
    job = CI["jobs"]["coverage"]
    assert job["name"] == "Coverage"
    assert "needs" not in job
    assert job["timeout-minutes"] == 7
    assert "schedule" in job["if"]
    step_names = [step["name"] for step in job["steps"]]
    assert "Does this change need a coverage run?" in step_names
    assert "Diff coverage of changed lines at least 85 percent" in step_names
    assert "Upload coverage to Codecov" in step_names
    upload = next(step for step in job["steps"] if step["name"] == "Upload coverage to Codecov")
    assert upload["uses"].startswith("codecov/codecov-action@")
    assert "codecov/codecov-action@303a32d7a59b442fa8d48b6a1cc6825c09c847a5 # v7.1.1" in CI_TEXT
    assert upload["with"]["files"] == "coverage.xml"
    assert "github.actor != 'dependabot[bot]'" in upload["if"]  # bots cannot read CODECOV_TOKEN; diff-cover still gates them
    diff = next(step for step in job["steps"] if step["name"].startswith("Diff coverage"))
    assert "--fail-under=85" in diff["run"]
    assert "pull_request" in diff["if"] and "merge_group" in diff["if"]


def test_coverage_job_is_catalogued_as_pipeline_only():
    assert pipeline_checks.unclassified_jobs(CI_TEXT) == []
    entry = next(item for item in pipeline_checks.PIPELINE_JOBS if item.key == "coverage")
    assert entry.name == "Coverage"
    assert entry.check_steps == ()
    assert "Codecov" in entry.pipeline_only_reason


def test_readme_has_required_status_badges():
    for fragment in (
        "actions/workflows/ci.yml/badge.svg",
        "codecov.io/gh/jooservices/ai-flow/branch/develop/graph/badge.svg",
        "actions/workflows/codeql.yml/badge.svg",
        "api.securityscorecards.dev/projects/github.com/jooservices/ai-flow/badge",
        "badge/python-3.12%2B-blue.svg",
        "github/v/release/jooservices/ai-flow",
        "github/license/jooservices/ai-flow",
    ):
        assert fragment in README


def test_topic_is_documented_and_linked():
    assert "diff-cover" in DOCS and "85" in DOCS and "Codecov" in DOCS
    assert "(../../docs/v4/coverage-gate.md)" in DEPLOY_README.split("## Topics", 1)[1]


def test_coverage_select_tests_keeps_smoke_and_stem_matches(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("BASE", "HEAD")
    # Comparing HEAD to HEAD yields no changed files; the helper still keeps the smoke modules.
    result = subprocess.run(
        ["python3", "deploy/v4/ci/coverage_select_tests.py"],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    paths = result.stdout.strip().split()
    assert "tests/v4/test_install_smoke.py" in paths
    assert "tests/v4/test_route_registry.py" in paths

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "coverage_select_tests",
        ROOT / "deploy/v4/ci/coverage_select_tests.py",
    )
    select = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(select)

    available = {
        "tests/v4/test_install_smoke.py",
        "tests/v4/test_route_registry.py",
        "tests/v4/test_pipeline_checks.py",
        "tests/v4/test_other.py",
    }
    chosen = select.select_paths(
        ["aiflow/v4/core/pipeline_checks.py", "tests/v4/test_other.py"],
        available,
    )
    assert chosen == [
        "tests/v4/test_install_smoke.py",
        "tests/v4/test_other.py",
        "tests/v4/test_pipeline_checks.py",
        "tests/v4/test_route_registry.py",
    ]
