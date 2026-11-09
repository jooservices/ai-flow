"""The browser journeys run only for console or browser test changes, and Test (v4) accepts their skip (JAIF-592)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
NO_NEW_COMMIT = "0" * 40


def _path_filter_script() -> str:
    return next(step["run"] for step in CI["jobs"]["changes"]["steps"] if step.get("id") == "paths")


def _aggregate_script() -> str:
    return CI["jobs"]["v4"]["steps"][0]["run"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _run_path_filter(tmp_path: Path, changed: list[str], event: str = "pull_request",
                     base: str | None = None) -> subprocess.CompletedProcess:
    """Run the filter step in a throwaway repository whose base commit has none of the changed paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()
    for path in changed:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("changed\n", encoding="utf-8")
    if changed:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "change")
    output = tmp_path / "github-output"
    env = {"PATH": os.environ["PATH"], "EVENT": event, "BASE": base or base_sha, "GITHUB_OUTPUT": str(output)}
    return subprocess.run(["bash", "-eo", "pipefail", "-c", _path_filter_script()], cwd=repo, env=env,
                          capture_output=True, text=True)


def _browser_output(tmp_path: Path, changed: list[str], event: str = "pull_request") -> str:
    result = _run_path_filter(tmp_path, changed, event)
    assert result.returncode == 0, result.stderr
    return (tmp_path / "github-output").read_text(encoding="utf-8")


def test_browser_job_waits_for_the_path_filter_and_the_filter_publishes_its_decision():
    job = CI["jobs"]["v4-browser"]
    assert job["needs"] == ["changes"]
    assert "needs.changes.outputs.browser == 'true'" in job["if"]
    assert CI["jobs"]["changes"]["outputs"]["browser"] == "${{ steps.paths.outputs.browser }}"


def test_required_check_waits_for_the_filter_and_accepts_a_skipped_browser_job():
    required = CI["jobs"]["v4"]
    assert set(required["needs"]) == {"changes", "v4-shard", "v4-browser"}
    assert required["if"].startswith("always()")
    assert required["steps"][0]["env"]["BROWSER_NEEDED"] == "${{ needs.changes.outputs.browser }}"


@pytest.mark.parametrize("changed", [
    ["aiflow/v4/control/ui/console.html"],
    ["tests/v4/test_console_browser.py"],
    ["tests/v4/test_log_viewer_browser.py"],
    ["tests/v4/console_browser_suite.cjs"],
    ["tests/v4/log_viewer_browser_suite.cjs"],
    ["docs/v4/x.md", "tests/v4/test_console_browser.py"],
])
def test_a_console_or_browser_test_change_runs_the_browser_journeys(tmp_path, changed):
    assert _browser_output(tmp_path, changed) == "browser=true\n"


@pytest.mark.parametrize("changed", [
    ["aiflow/v4/control/api.py", "docs/v4/x.md"],
    ["aiflow/v4/control/ui_helpers.py"],
    ["tests/v4/test_console.py"],
    ["tests/v4/test_console_browser.py.orig"],
])
def test_other_changes_skip_the_browser_journeys(tmp_path, changed):
    assert _browser_output(tmp_path, changed) == "browser=false\n"


def test_a_push_always_runs_the_browser_journeys(tmp_path):
    assert _browser_output(tmp_path, ["docs/v4/x.md"], event="push") == "browser=true\n"


def test_the_filter_fails_closed_when_the_base_commit_is_unknown(tmp_path):
    result = _run_path_filter(tmp_path, ["docs/v4/x.md"], base=NO_NEW_COMMIT)
    assert result.returncode != 0
    assert not (tmp_path / "github-output").exists()


@pytest.mark.parametrize(("changes", "shards", "browser", "browser_needed", "passes"), [
    ("success", "success", "skipped", "false", True),
    ("success", "success", "success", "true", True),
    ("success", "success", "skipped", "true", False),
    ("success", "success", "failure", "true", False),
    ("success", "success", "failure", "false", False),
    ("success", "failure", "skipped", "false", False),
    ("failure", "skipped", "skipped", "false", False),
])
def test_the_required_check_passes_only_when_every_needed_job_passed(changes, shards, browser, browser_needed, passes):
    env = {"PATH": os.environ["PATH"], "CHANGES": changes, "SHARDS": shards, "BROWSER": browser,
           "BROWSER_NEEDED": browser_needed}
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", _aggregate_script()], env=env,
                            capture_output=True, text=True)
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
