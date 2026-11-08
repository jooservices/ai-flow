"""Select focused v4 tests for a local change, with a full-suite fallback.

    uv run --frozen python -m aiflow.v4.core.affected_tests --base origin/develop

The helper prints the selected pytest paths and the estimated test-file reduction. Shared, unknown or unmapped code
always selects the complete suite. CI continues to run the complete configured test suite.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

TEST_ROOT = Path("tests/v4")
SMOKE_TESTS = ("tests/v4/test_install_smoke.py", "tests/v4/test_route_registry.py")
TOOLING_TESTS: dict[str, tuple[str, ...]] = {
    "aiflow/v4/core/affected_tests.py": ("test_affected_tests.py",),
    "aiflow/v4/core/docsite.py": ("test_docsite_views_parity.py", "test_documentation.py"),
}
FULL_SUITE_PATHS = frozenset({
    "aiflow/v4/control/api.py",
    "aiflow/v4/control/app_helpers.py",
    "aiflow/v4/control/db.py",
    "aiflow/v4/control/layouts.py",
    "aiflow/v4/control/auth.py",
})

# Each code path is listed only when its tests have been reviewed as a focused set. Add a new source path here when
# its feature tests are known; ambiguous and shared modules intentionally fall back to all tests.
FILE_TESTS: dict[str, tuple[str, ...]] = {
    "aiflow/v4/control/dashboard_silent_routes.py": ("test_silent_runs.py", "test_dashboard_timeouts.py"),
    "aiflow/v4/control/features/review_rounds/models.py": ("test_review_rounds.py",),
    "aiflow/v4/control/features/review_rounds/repository.py": ("test_review_rounds.py", "test_ticket_repository.py"),
    "aiflow/v4/control/features/review_rounds/service.py": ("test_review_rounds.py", "test_master_review_triggers.py"),
    "aiflow/v4/control/features/work/claim_extras.py": ("test_claim_steps.py", "test_master_action_steps.py"),
    "aiflow/v4/control/features/work/eligibility_candidates.py": (
        "test_eligibility_rules.py", "test_eligibility_sharing.py", "test_worker_candidates.py"),
    "aiflow/v4/control/features/work/eligible.py": (
        "test_eligibility_rules.py", "test_eligibility_sharing.py", "test_worker_candidates.py"),
    "aiflow/v4/control/features/work/repository.py": ("test_ticket_repository.py", "test_claim_steps.py"),
    "aiflow/v4/control/incidents.py": ("test_flow_failure_roots.py", "test_incidents_idle_parity.py", "test_idle_flow.py"),
    "aiflow/v4/control/jira.py": ("test_route_module_jira.py", "test_jira_connect.py", "test_jira_versions.py"),
    "aiflow/v4/control/repositories/dashboard.py": (
        "test_route_module_dashboard.py", "test_dashboard_timeouts.py", "test_production_health_dashboard.py",
        "test_master_review_triggers.py", "test_review_rounds.py"),
    "aiflow/v4/control/repositories/developer_runs.py": ("test_silent_runs.py",),
    "aiflow/v4/control/routes/dashboard.py": (
        "test_route_module_dashboard.py", "test_dashboard_timeouts.py", "test_production_health_dashboard.py",
        "test_master_review_triggers.py", "test_review_rounds.py"),
    "aiflow/v4/control/ticket_split_apply.py": ("test_ticket_split_apply.py", "test_ticket_splitting.py"),
    "aiflow/v4/control/analytics.py": ("test_analytics.py", "test_route_module_analytics.py"),
    "aiflow/v4/control/features/runs/master_cycles.py": ("test_master.py", "test_master_review_triggers.py"),
    "aiflow/v4/control/features/runs/routes.py": ("test_work_routes_install_parity.py", "test_master.py"),
    "aiflow/v4/control/repositories/master_cycles.py": ("test_master.py", "test_master_review_triggers.py"),
    "aiflow/v4/control/watchdog.py": (
        "test_watchdog_checks.py", "test_idle_flow.py", "test_escalation_deadline.py", "test_retry_hold.py"),
    "aiflow/v4/control/repositories/watchdog.py": (
        "test_watchdog_checks.py", "test_idle_flow.py", "test_escalation_deadline.py", "test_retry_hold.py"),
    "aiflow/v4/worker/dev.py": ("test_dev_run_steps.py", "test_silent_runs.py", "test_covering_tests_slice.py"),
    "aiflow/v4/worker/impact.py": ("test_adapter_run_parity.py", "test_runs_autonomy_parity.py", "test_worker_loop_cycle_parity.py"),
    "aiflow/v4/worker/intake.py": ("test_intake.py", "test_intake_split.py"),
    "aiflow/v4/worker/master.py": (
        "test_master.py", "test_master_run_parity.py", "test_master_action_steps.py",
        "test_master_escalation_audit_events.py", "test_master_review_requests.py", "test_master_review_triggers.py"),
    "aiflow/v4/worker/verify.py": (
        "test_reviews.py", "test_review_findings.py", "test_review_rounds.py", "test_review_metrics_parity.py"),
    "aiflow/v4/install/promotion.py": ("test_release.py", "test_upgrade_layout.py", "test_clean_install_acceptance.py"),
    "aiflow/v4/install/secrets.py": ("test_release.py", "test_upgrade_layout.py", "test_clean_install_acceptance.py"),
    "aiflow/v4/node/operations.py": (
        "test_server_installer_rollback.py", "test_node_operations.py", "test_node_self_update_parity.py"),
}

PREFIX_TESTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("aiflow/v4/control/watchdog_checks/", (
        "test_watchdog_checks.py", "test_idle_flow.py", "test_escalation_deadline.py", "test_retry_hold.py")),
    ("aiflow/v4/control/features/fleet/", (
        "test_fleet_install.py", "test_fleet_audit_events.py", "test_fleet_audit_in_events.py",
        "test_fleet_listeners.py", "test_fleet_upgrade.py", "test_route_install_registrations.py")),
    ("aiflow/v4/control/features/github_fenced/", (
        "test_github_fenced_install_parity.py", "test_github_connections.py", "test_github_connect.py",
        "test_route_install_registrations.py")),
)
NON_CODE_TESTS: dict[str, tuple[str, ...]] = {
    "deploy/v4/README.md": ("test_documentation.py",),
    "deploy/v4/topics/upgrade-layout.md": ("test_upgrade_layout.py",),
    "docs/v4/affected-tests.md": ("test_affected_tests.py",),
    "docs/v4/console-layouts.md": ("test_console_layouts.py", "test_console.py"),
    "docs/v4/node-operations.md": ("test_node_operations.py", "test_server_installer_rollback.py"),
    "docs/v4/review-rounds.md": ("test_review_rounds.py", "test_console.py", "test_console_review_rounds.py"),
    "docs/v4/upgrade-layout.md": ("test_upgrade_layout.py",),
    "prompts/v4/dev.md": ("test_covering_tests_slice.py", "test_dev_run_steps.py"),
    "prompts/v4/intake.md": ("test_intake.py", "test_intake_split.py"),
    "prompts/v4/verify.md": ("test_reviews.py", "test_review_findings.py", "test_review_rounds.py"),
    "deploy/v4/bin/aiflow4-autodeploy": ("test_release.py", "test_clean_install_acceptance.py"),
    "deploy/v4/bin/aiflow4-autodeploy-develop": ("test_release.py", "test_clean_install_acceptance.py"),
    "deploy/v4/bin/aiflow4-deploy": ("test_release.py", "test_clean_install_acceptance.py"),
    "deploy/v4/bin/aiflow4-deploy-develop": ("test_release.py", "test_clean_install_acceptance.py"),
}


@dataclass(frozen=True)
class Selection:
    tests: tuple[str, ...]
    full_suite: bool
    reason: str


def test_files(root: Path) -> tuple[str, ...]:
    """Return all v4 pytest modules relative to the repository root."""
    return tuple(sorted(path.relative_to(root).as_posix() for path in (root / TEST_ROOT).rglob("test_*.py")))


def select_tests(changed_files: Iterable[str], root: Path = Path(".")) -> Selection:
    """Map known changed files to focused tests; use the full suite whenever the map is uncertain."""
    available = set(test_files(root))
    full = tuple(sorted(available))
    changed = tuple(sorted(set(changed_files)))
    if not changed:
        return Selection(full, True, "no changed files were found")
    selected = {path for path in SMOKE_TESTS if path in available}
    for name in changed:
        mapped = TOOLING_TESTS.get(name)
        if mapped is not None:
            mapped_paths = {f"tests/v4/{test}" for test in mapped}
            if not mapped_paths.issubset(available):
                return Selection(full, True, f"mapped test missing for {name}")
            selected.update(mapped_paths)
            continue
        if name in FULL_SUITE_PATHS or name.startswith("aiflow/v4/core/") or name.startswith("aiflow/v4/control/migrations/"):
            return Selection(full, True, f"shared code changed: {name}")
        if name.startswith("tests/v4/test_") and name.endswith(".py") and name in available:
            selected.add(name)
            continue
        mapped = NON_CODE_TESTS.get(name)
        if mapped is not None:
            mapped_paths = {f"tests/v4/{test}" for test in mapped}
            if not mapped_paths.issubset(available):
                return Selection(full, True, f"mapped test missing for {name}")
            selected.update(mapped_paths)
            continue
        mapped = FILE_TESTS.get(name)
        if mapped is None:
            mapped = next((tests for prefix, tests in PREFIX_TESTS if name.startswith(prefix)), None)
        if mapped is None:
            return Selection(full, True, f"no reviewed test map for {name}")
        mapped_paths = {f"tests/v4/{test}" for test in mapped}
        if not mapped_paths.issubset(available):
            return Selection(full, True, f"mapped test missing for {name}")
        selected.update(mapped_paths)
    return Selection(tuple(sorted(selected)), False, "all changed files have reviewed test mappings")


def changed_files(root: Path, base: str) -> tuple[str, ...]:
    """Read tracked changes from a base and add untracked files outside temporary scratch space."""
    diff = subprocess.run(["git", "diff", "--name-only", "--diff-filter=ACDMRT", base, "--"], cwd=root,
                          capture_output=True, text=True, check=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
                               capture_output=True, text=True, check=True)
    paths = {line for line in diff.stdout.splitlines() if line}
    paths.update(line for line in untracked.stdout.splitlines() if line and not line.startswith(".tmp/"))
    return tuple(sorted(paths))


def format_report(selection: Selection, total_files: int) -> str:
    """Format a short plan with focused paths or the full-suite command."""
    chosen = len(selection.tests)
    saved = max(total_files - chosen, 0)
    percent = round(saved * 100 / total_files) if total_files else 0
    if selection.full_suite:
        return f"FULL SUITE: {selection.reason}\nRun: uv run --frozen pytest tests/v4 ({total_files} test files)."
    mode = f"FOCUSED ({percent}% fewer test files)"
    paths = " ".join(selection.tests)
    return (f"{mode}: {selection.reason}\nSelected {chosen}/{total_files} test files; {saved} omitted.\n"
            f"Run: uv run --frozen pytest {paths}")


def run_tests(selection: Selection, root: Path, full_suite_seconds: float | None = None) -> int:
    """Run the selected tests and report elapsed time and savings when a full-suite baseline is given."""
    try:
        database = urlsplit(os.environ.get("AIFLOW4_TEST_DATABASE_URL", ""))
        username = unquote(database.username or "")
        database_name = unquote(database.path.removeprefix("/"))
    except ValueError:
        username, database_name = "", ""
    suffix = database_name.rpartition("_wt")[2]
    worktree_database = bool(suffix) and suffix.isalnum()
    test_database = database_name.endswith("_test") or worktree_database
    if not username.endswith("_test") or not test_database or not os.environ.get("AIFLOW4_TEST_REDIS_URL"):
        print("Refusing to run tests: configure the project's *_test PostgreSQL role and test Redis URL.")
        return 2
    targets = ("tests/v4",) if selection.full_suite else selection.tests
    baseline_path = root / ".pytest_cache" / "affected-tests-full-suite-seconds"
    baseline = full_suite_seconds
    if baseline is None:
        try:
            baseline = float(baseline_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            baseline = None
    started = time.monotonic()
    result = subprocess.run(["uv", "run", "--frozen", "pytest", *targets], cwd=root)
    elapsed = time.monotonic() - started
    if selection.full_suite and result.returncode == 0:
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(f"{elapsed:.6f}\n", encoding="utf-8")
        except OSError:
            pass  # The report still shows the full-run duration when the optional local cache is read-only.
        print(f"Elapsed: {elapsed:.1f}s; full-suite timing recorded.")
    elif baseline is None:
        print(f"Elapsed: {elapsed:.1f}s; time saved: unknown (no full-suite timing supplied).")
    else:
        saved = max(baseline - elapsed, 0.0)
        print(f"Elapsed: {elapsed:.1f}s; time saved vs {baseline:.1f}s full-suite baseline: {saved:.1f}s.")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="affected_tests", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="origin/develop", help="git ref to compare against")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--plan-only", action="store_true", help="print the selection without running pytest")
    parser.add_argument("--full-suite-seconds", type=float, help="measured full-suite time for savings comparison")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        changed = changed_files(root, args.base)
    except (OSError, subprocess.CalledProcessError) as exc:
        selection = Selection(test_files(root), True, f"cannot determine changed files ({exc})")
    else:
        selection = select_tests(changed, root)
    print(format_report(selection, len(test_files(root))))
    if args.plan_only:
        return 0
    try:
        return run_tests(selection, root, args.full_suite_seconds)
    except OSError as exc:
        print(f"Test command could not start: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
