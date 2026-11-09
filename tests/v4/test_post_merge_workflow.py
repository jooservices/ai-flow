"""Pin the post-merge workflow: it runs on pushes to develop and master only and is no pull request check."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci-post-merge.yml"
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers() -> dict:
    # PyYAML's YAML 1.1 resolver parses the unquoted key `on` as boolean True.
    workflow = _workflow()
    return workflow["on"] if "on" in workflow else workflow[True]


def test_runs_only_on_push_to_develop_and_master():
    triggers = _triggers()
    assert set(triggers) == {"push"}
    assert triggers["push"] == {"branches": ["master", "develop"]}


def test_is_not_a_pull_request_or_merge_queue_check():
    triggers = _triggers()
    assert "pull_request" not in triggers
    assert "merge_group" not in triggers


def test_runs_the_unit_tests_and_every_v4_shard():
    jobs = _workflow()["jobs"]
    unit_commands = " ".join(step.get("run", "") for step in jobs["unit"]["steps"])
    assert "unittest discover -s tests" in unit_commands
    assert jobs["v4-shard"]["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    shard_commands = " ".join(step.get("run", "") for step in jobs["v4-shard"]["steps"])
    assert "test_shards files --index ${{ matrix.shard }} --total 4" in shard_commands


def test_reports_a_failure_with_a_bug_ticket():
    report = _workflow()["jobs"]["report"]
    assert report["needs"] == ["unit", "v4-shard"]
    assert report["if"] == "failure()"
    assert report["permissions"]["issues"] == "write"
    report_commands = " ".join(step.get("run", "") for step in report["steps"])
    assert "gh issue create --repo" in report_commands
    assert "--label bug" in report_commands


def test_every_action_is_pinned_to_a_commit():
    uses = [step["uses"] for job in _workflow()["jobs"].values() for step in job["steps"] if "uses" in step]
    assert uses
    for reference in uses:
        assert PINNED_ACTION.match(reference), reference


def test_shard_count_matches_the_main_pipeline():
    main = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert main["jobs"]["v4-shard"]["strategy"]["matrix"] == _workflow()["jobs"]["v4-shard"]["strategy"]["matrix"]
