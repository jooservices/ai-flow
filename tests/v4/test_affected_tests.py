"""JAIF-583: reviewed feature mappings, smoke tests and conservative full-suite fallback."""
from __future__ import annotations

import subprocess
from subprocess import CompletedProcess
from pathlib import Path

from aiflow.v4.core import affected_tests as selector

ROOT = Path(__file__).resolve().parents[2]


def test_shared_core_change_selects_the_complete_suite():
    result = selector.select_tests(["aiflow/v4/core/text.py"], ROOT)
    assert result.full_suite
    assert result.tests == selector.test_files(ROOT)


def test_declared_mappings_reference_existing_test_modules():
    mappings = [*selector.TOOLING_TESTS.values(), *selector.FILE_TESTS.values(), *selector.NON_CODE_TESTS.values()]
    mappings.extend(tests for _, tests in selector.PREFIX_TESTS)
    assert all((ROOT / "tests/v4" / test).is_file() for tests in mappings for test in tests)


def test_missing_mapped_test_falls_back_to_every_available_test(tmp_path):
    test_root = tmp_path / "tests" / "v4"
    test_root.mkdir(parents=True)
    for name in selector.SMOKE_TESTS:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()
    result = selector.select_tests(["aiflow/v4/worker/dev.py"], tmp_path)
    assert result.full_suite
    assert "mapped test missing" in result.reason


def test_selector_tool_change_uses_its_unit_tests_and_smoke_checks():
    result = selector.select_tests(["aiflow/v4/core/affected_tests.py"], ROOT)
    assert not result.full_suite
    assert set(result.tests) == {
        "tests/v4/test_affected_tests.py", "tests/v4/test_install_smoke.py", "tests/v4/test_route_registry.py",
    }


def test_documentation_tool_change_uses_documentation_tests_and_smoke_checks():
    result = selector.select_tests(["aiflow/v4/core/docsite.py"], ROOT)
    assert not result.full_suite
    assert "tests/v4/test_docsite_views_parity.py" in result.tests
    assert "tests/v4/test_documentation.py" in result.tests


def test_node_installer_change_maps_to_installer_and_update_tests():
    result = selector.select_tests(["aiflow/v4/node/operations.py"], ROOT)
    assert not result.full_suite
    assert "tests/v4/test_server_installer_rollback.py" in result.tests
    assert "tests/v4/test_node_operations.py" in result.tests


def test_feature_directory_change_maps_to_its_feature_test_group():
    result = selector.select_tests(["aiflow/v4/control/features/fleet/routes_accounts.py"], ROOT)
    assert not result.full_suite
    assert "tests/v4/test_fleet_install.py" in result.tests
    assert "tests/v4/test_fleet_upgrade.py" in result.tests


def test_documentation_and_prompt_changes_map_to_covering_tests():
    result = selector.select_tests(["docs/v4/review-rounds.md", "prompts/v4/verify.md"], ROOT)
    assert not result.full_suite
    assert "tests/v4/test_review_rounds.py" in result.tests
    assert "tests/v4/test_reviews.py" in result.tests
    assert "tests/v4/test_install_smoke.py" in result.tests


def test_console_documentation_change_selects_console_regression_tests():
    result = selector.select_tests(["docs/v4/console-layouts.md", "docs/v4/review-rounds.md"], ROOT)
    assert not result.full_suite
    assert "tests/v4/test_console.py" in result.tests
    assert "tests/v4/test_console_review_rounds.py" in result.tests


def test_known_feature_change_selects_its_tests_and_smoke_checks():
    result = selector.select_tests(["aiflow/v4/worker/dev.py"], ROOT)
    assert not result.full_suite
    assert set(result.tests) == {
        "tests/v4/test_covering_tests_slice.py", "tests/v4/test_dev_run_steps.py", "tests/v4/test_install_smoke.py",
        "tests/v4/test_route_registry.py", "tests/v4/test_silent_runs.py",
    }


def test_unknown_code_change_falls_back_to_the_complete_suite():
    result = selector.select_tests(["aiflow/v4/worker/new_feature.py"], ROOT)
    assert result.full_suite
    assert "no reviewed test map" in result.reason


def test_changed_test_module_is_included_with_smoke_checks():
    result = selector.select_tests(["tests/v4/test_fencing.py"], ROOT)
    assert not result.full_suite
    assert set(result.tests) == {
        "tests/v4/test_fencing.py", "tests/v4/test_install_smoke.py", "tests/v4/test_route_registry.py",
    }


def test_empty_change_set_falls_back_to_the_complete_suite():
    result = selector.select_tests([], ROOT)
    assert result.full_suite
    assert "no changed files" in result.reason


def test_report_states_test_file_reduction():
    selection = selector.Selection(("tests/v4/test_fencing.py",), False, "mapped")
    report = selector.format_report(selection, 4)
    assert "FOCUSED (75% fewer test files)" in report
    assert "Selected 1/4 test files; 3 omitted." in report


def test_full_suite_report_stays_short():
    selection = selector.Selection(("tests/v4/test_fencing.py", "tests/v4/test_api.py"), True, "shared code")
    report = selector.format_report(selection, 241)
    assert report == "FULL SUITE: shared code\nRun: uv run --frozen pytest tests/v4 (241 test files)."


def test_main_prints_the_selection_without_running_pytest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(selector, "changed_files", lambda root, base: ("aiflow/v4/worker/dev.py",))
    monkeypatch.setattr(selector, "test_files", lambda root: (
        "tests/v4/test_covering_tests_slice.py", "tests/v4/test_dev_run_steps.py", "tests/v4/test_install_smoke.py",
        "tests/v4/test_route_registry.py", "tests/v4/test_silent_runs.py"))
    assert selector.main(["--root", str(tmp_path), "--plan-only"]) == 0
    assert "FOCUSED" in capsys.readouterr().out


def test_run_tests_reports_elapsed_and_time_saved(tmp_path, monkeypatch, capsys):
    clock = iter((10.0, 12.5))
    calls = []
    monkeypatch.setenv("AIFLOW4_TEST_DATABASE_URL", "postgresql://aiflow_test@localhost/aiflow4_wt583")
    monkeypatch.setenv("AIFLOW4_TEST_REDIS_URL", "redis://localhost:6379/5")
    monkeypatch.setattr(selector.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(selector.subprocess, "run", lambda command, cwd: (
        calls.append((command, cwd)) or CompletedProcess(command, 0)))
    selection = selector.Selection(("tests/v4/test_fencing.py",), False, "mapped")
    assert selector.run_tests(selection, tmp_path, 20.0) == 0
    assert calls == [(["uv", "run", "--frozen", "pytest", "tests/v4/test_fencing.py"], tmp_path)]
    assert "Elapsed: 2.5s; time saved vs 20.0s full-suite baseline: 17.5s." in capsys.readouterr().out


def test_successful_full_suite_records_a_timing_baseline(tmp_path, monkeypatch, capsys):
    clock = iter((30.0, 40.0))
    monkeypatch.setenv("AIFLOW4_TEST_DATABASE_URL", "postgresql://aiflow_test@localhost/aiflow4_wt583")
    monkeypatch.setenv("AIFLOW4_TEST_REDIS_URL", "redis://localhost:6379/5")
    monkeypatch.setattr(selector.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(selector.subprocess, "run", lambda command, cwd: CompletedProcess(command, 0))
    selection = selector.Selection(("tests/v4/test_fencing.py",), True, "shared code")
    assert selector.run_tests(selection, tmp_path) == 0
    saved = (tmp_path / ".pytest_cache" / "affected-tests-full-suite-seconds").read_text()
    assert float(saved) == 10.0
    assert "Elapsed: 10.0s; full-suite timing recorded." in capsys.readouterr().out


def test_run_tests_rejects_an_admin_database_role(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("AIFLOW4_TEST_DATABASE_URL", "postgresql://workspaces_admin@localhost/aiflow4_wt583")
    monkeypatch.setenv("AIFLOW4_TEST_REDIS_URL", "redis://localhost:6379/5")
    monkeypatch.setattr(selector.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    selection = selector.Selection(("tests/v4/test_fencing.py",), False, "mapped")
    assert selector.run_tests(selection, tmp_path) == 2
    assert not calls
    assert "Refusing to run tests" in capsys.readouterr().out


def test_untracked_file_collection_ignores_temporary_brief(tmp_path, monkeypatch):
    results = [
        subprocess.CompletedProcess([], 0, stdout="aiflow/v4/worker/dev.py\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="tests/v4/test_added.py\n.tmp/brief.md\n", stderr=""),
    ]
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return results[len(calls) - 1]

    monkeypatch.setattr(selector.subprocess, "run", fake_run)
    assert selector.changed_files(tmp_path, "origin/develop") == (
        "aiflow/v4/worker/dev.py", "tests/v4/test_added.py")
    assert calls[0][:4] == ["git", "diff", "--name-only", "--diff-filter=ACDMRT"]
