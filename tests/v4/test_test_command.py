"""Tests for the one test command covering all quality layers (JAIF-579)."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.v4.conftest import pytest_collection_modifyitems

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "test"


def _load_script_module():
    """Import scripts/test dynamically as a Python module for unit testing."""
    loader = SourceFileLoader("scripts_test", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader("scripts_test", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_separate_layers_and_paths_defaults_to_all_layers_when_no_layer_is_specified():
    """Verify that omitting layer names selects all four layers in the defined order."""
    runner = _load_script_module()
    layers, paths = runner._separate_layers_and_paths(["tests/v4/test_cli.py"])
    assert layers == ["fast", "integration", "smoke", "e2e"]
    assert paths == ["tests/v4/test_cli.py"]


def test_separate_layers_and_paths_filters_to_requested_layers_preserving_order():
    """Verify that specifying layer names selects only those requested layers."""
    runner = _load_script_module()
    layers, paths = runner._separate_layers_and_paths(["smoke", "fast", "tests/v4/test_cli.py"])
    assert layers == ["smoke", "fast"]
    assert paths == ["tests/v4/test_cli.py"]


def test_clean_run_reports_each_layer_with_short_pass_output(capsys, monkeypatch):
    """Verify that a successful run outputs a single PASS line per layer and exits zero."""
    runner = _load_script_module()
    monkeypatch.setattr(runner, "run_fast_layer", lambda root, base, paths: (True, ""))
    monkeypatch.setattr(runner, "run_integration_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_smoke_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_e2e_layer", lambda root, paths: (True, ""))

    exit_code = runner.main([])
    captured = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 0
    assert captured == [
        "fast: PASS",
        "integration: PASS",
        "smoke: PASS",
        "e2e: PASS",
    ]


def test_deliberately_broken_unit_test_fails_fast_layer_while_other_layers_report(capsys, monkeypatch):
    """Verify that a failing fast layer reports FAIL and later layers still report."""
    runner = _load_script_module()
    monkeypatch.setattr(runner, "run_fast_layer", lambda root, base, paths: (False, "AssertionError in unit test"))
    monkeypatch.setattr(runner, "run_integration_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_smoke_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_e2e_layer", lambda root, paths: (True, ""))

    exit_code = runner.main([])
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "fast: FAIL" in captured
    assert "integration: PASS" in captured
    assert "smoke: PASS" in captured
    assert "e2e: PASS" in captured
    assert "--- fast failure ---" in captured
    assert "AssertionError in unit test" in captured


def test_deliberately_broken_integration_test_fails_integration_layer(capsys, monkeypatch):
    """Verify that an integration failure reports FAIL for integration and passes others."""
    runner = _load_script_module()
    monkeypatch.setattr(runner, "run_fast_layer", lambda root, base, paths: (True, ""))
    monkeypatch.setattr(runner, "run_integration_layer", lambda root, paths: (False, "Database connection refused"))
    monkeypatch.setattr(runner, "run_smoke_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_e2e_layer", lambda root, paths: (True, ""))

    exit_code = runner.main([])
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "fast: PASS" in captured
    assert "integration: FAIL" in captured
    assert "smoke: PASS" in captured
    assert "e2e: PASS" in captured
    assert "--- integration failure ---" in captured
    assert "Database connection refused" in captured


def test_deliberately_broken_smoke_test_fails_smoke_layer(capsys, monkeypatch):
    """Verify that a broken smoke test fails only the smoke layer."""
    runner = _load_script_module()
    monkeypatch.setattr(runner, "run_fast_layer", lambda root, base, paths: (True, ""))
    monkeypatch.setattr(runner, "run_integration_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_smoke_layer", lambda root, paths: (False, "Smoke endpoint 500 error"))
    monkeypatch.setattr(runner, "run_e2e_layer", lambda root, paths: (True, ""))

    exit_code = runner.main([])
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "fast: PASS" in captured
    assert "integration: PASS" in captured
    assert "smoke: FAIL" in captured
    assert "e2e: PASS" in captured
    assert "--- smoke failure ---" in captured
    assert "Smoke endpoint 500 error" in captured


def test_deliberately_broken_e2e_test_fails_e2e_layer(capsys, monkeypatch):
    """Verify that a broken e2e test fails only the e2e layer."""
    runner = _load_script_module()
    monkeypatch.setattr(runner, "run_fast_layer", lambda root, base, paths: (True, ""))
    monkeypatch.setattr(runner, "run_integration_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_smoke_layer", lambda root, paths: (True, ""))
    monkeypatch.setattr(runner, "run_e2e_layer", lambda root, paths: (False, "Playwright selector timed out"))

    exit_code = runner.main([])
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "fast: PASS" in captured
    assert "integration: PASS" in captured
    assert "smoke: PASS" in captured
    assert "e2e: FAIL" in captured
    assert "--- e2e failure ---" in captured
    assert "Playwright selector timed out" in captured


def test_fail_fast_flag_stops_execution_after_first_failing_layer(capsys, monkeypatch):
    """Verify that --fail-fast stops running remaining layers when an earlier layer fails."""
    runner = _load_script_module()
    integration_called = False

    def fake_integration(root, paths):
        nonlocal integration_called
        integration_called = True
        return True, ""

    monkeypatch.setattr(runner, "run_fast_layer", lambda root, base, paths: (False, "Fast failed"))
    monkeypatch.setattr(runner, "run_integration_layer", fake_integration)

    exit_code = runner.main(["-x"])
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "fast: FAIL" in captured
    assert "integration:" not in captured
    assert not integration_called


def test_single_layer_invocation_reports_only_requested_layer(capsys, monkeypatch):
    """Verify that running a single layer executes and prints output only for that layer."""
    runner = _load_script_module()
    monkeypatch.setattr(runner, "run_smoke_layer", lambda root, paths: (True, ""))

    exit_code = runner.main(["smoke"])
    captured = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert captured == "smoke: PASS"


def test_pytest_conftest_auto_assigns_layer_markers_when_missing():
    """Verify that conftest modifies items to assign appropriate layer markers."""
    class DummyItem:
        def __init__(self, nodeid: str, fixtures: tuple[str, ...] = ()):
            self.nodeid = nodeid
            self.fixturenames = fixtures
            self.markers: list[str] = []

        def iter_markers(self):
            return [SimpleNamespace(name=m) for m in self.markers]

        def add_marker(self, marker):
            name = getattr(marker, "name", str(marker))
            self.markers.append(name)

    unit_item = DummyItem("tests/v4/test_commit_rules.py::test_rule")
    integration_item = DummyItem("tests/v4/test_custom.py::test_repo", fixtures=("db",))
    smoke_item = DummyItem("tests/v4/test_install_smoke.py::test_smoke")
    e2e_item = DummyItem("tests/v4/test_console_browser.py::test_login")

    pytest_collection_modifyitems(MagicMock(), [unit_item, integration_item, smoke_item, e2e_item])

    assert "unit" in unit_item.markers
    assert "integration" in integration_item.markers
    assert "smoke" in smoke_item.markers
    assert "e2e" in e2e_item.markers


def test_cli_smoke_layer_runs_as_executable_script():
    """Verify that ./scripts/test executes directly from disk and reports smoke PASS."""
    proc = _load_script_module()._run_process(
        [str(SCRIPT_PATH), "smoke", "tests/v4/test_install_smoke.py"],
        cwd=ROOT,
    )
    exit_code, output = proc
    assert exit_code == 0
    assert output.strip() == "smoke: PASS"


def test_cli_deliberately_broken_test_fails_proper_layer(tmp_path):
    """Verify that running a deliberately broken test fails the layer and reports failure tail."""
    broken_file = tmp_path / "test_broken_smoke.py"
    broken_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.smoke\n"
        "def test_intentional_smoke_failure():\n"
        "    assert 1 == 2, 'intentional smoke test failure'\n"
    )

    proc = _load_script_module()._run_process(
        [str(SCRIPT_PATH), "smoke", str(broken_file)],
        cwd=ROOT,
    )
    exit_code, output = proc
    assert exit_code == 1
    assert "smoke: FAIL" in output
    assert "--- smoke failure ---" in output
    assert "intentional smoke test failure" in output

