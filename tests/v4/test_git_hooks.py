"""The git hooks (JAIF-581): the pre-commit checks, the affected tests at push, and install in a fresh worktree."""
from __future__ import annotations

import os
import shutil
import json
import subprocess
import sys
import io
from pathlib import Path

import pytest

from aiflow.v4.core import git_hooks
from aiflow.v4.core import local_gate as gate
from aiflow.v4.core.local_gate import Context, GateResult, StepResult

ROOT = Path(__file__).resolve().parents[2]
GUARD_BASELINES = ("docs/v4/duplicate-blocks-baseline.json", "docs/v4/broad-except-baseline.json",
                   "docs/v4/sql-outside-data-access-baseline.json")
GITLEAKS_CONFIG = "deploy/v4/ci/gitleaks.toml"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture(autouse=True)
def _clear_inherited_hook_skip(monkeypatch):
    monkeypatch.delenv(git_hooks.SKIP_VARIABLE, raising=False)


def hook_env(**updates):
    env = os.environ.copy()
    env.pop(git_hooks.SKIP_VARIABLE, None)
    env.update(updates)
    return env


def git(path, *args, env=None):
    base = {"PATH": os.environ["PATH"], "HOME": str(path), **IDENTITY}
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True,
                          env={**base, **(env or {})})


def repo_on_develop(path: Path, files: dict[str, str] | None = None) -> Path:
    """A repository whose ``origin/develop`` is an initial commit with the guard baselines and ``files``."""
    git(path, "init", "-q", "-b", "develop")
    for baseline in (*GUARD_BASELINES, GITLEAKS_CONFIG):
        (path / baseline).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / baseline, path / baseline)
    for name, text in (files or {}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git(path, "add", ".")
    git(path, "commit", "-q", "--no-verify", "-m", "chore: Start the repository")
    git(path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    return path


def install_hook_files(path: Path) -> None:
    (path / ".githooks").mkdir(parents=True, exist_ok=True)
    for name in ("pre-commit", "commit-msg", "pre-push"):
        shutil.copy2(ROOT / ".githooks" / name, path / ".githooks" / name)


def test_affected_tests_are_the_changed_test_files_and_the_tests_named_after_changed_modules():
    changed = ["tests/v4/test_x.py", "aiflow/v4/core/local_gate.py", "aiflow/v4/worker/unknown_mod.py",
               "docs/v4/x.md", ".githooks/pre-push", "tests/v4/deleted_file.py"]
    existing = {"tests/v4/test_x.py", "tests/v4/test_local_gate.py", "tests/v4/test_git_hooks.py"}
    affected, unmapped = git_hooks.affected_test_files(changed, existing)
    assert affected == ["tests/v4/test_git_hooks.py", "tests/v4/test_local_gate.py", "tests/v4/test_x.py"]
    assert unmapped == ["aiflow/v4/worker/unknown_mod.py"]


def test_a_push_that_changes_no_v4_test_or_module_runs_only_the_smoke_tests(tmp_path):
    repo = repo_on_develop(tmp_path, {"README.md": "x"})
    (repo / "docs.md").write_text("y")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "docs: Add a note")
    plan = git_hooks.pre_push_plan(Context(repo, "develop"))
    assert [step.name for step in plan] == ["smoke"]
    assert plan[0].argv[-2:] == ("-p", "no:warnings") and "tests/v4/test_architecture.py" in plan[0].argv


def test_a_push_runs_the_tests_of_the_changed_module_after_the_smoke_tests(tmp_path):
    repo = repo_on_develop(tmp_path, {"tests/v4/test_local_gate.py": "def test_x():\n    pass\n"})
    (repo / "aiflow/v4/core").mkdir(parents=True)
    (repo / "aiflow/v4/core/local_gate.py").write_text("X = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "fix: Change the gate")
    plan = git_hooks.pre_push_plan(Context(repo, "develop"))
    assert [step.name for step in plan] == ["smoke", "affected-tests"]
    assert "tests/v4/test_local_gate.py" in plan[1].argv and plan[1].needs_env == gate.TEST_SERVICES


def test_a_push_uses_the_destination_sha_even_when_origin_base_is_already_at_head(tmp_path):
    repo = repo_on_develop(tmp_path, {"tests/v4/test_local_gate.py": "def test_x():\n    pass\n"})
    remote_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "tests/v4/test_local_gate.py").write_text("def test_x():\n    assert True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "test: Update the gate test")
    local_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "update-ref", "refs/remotes/origin/develop", local_sha)

    plan = git_hooks.pre_push_plan(
        Context(repo, "develop"),
        [("refs/heads/feature", local_sha, "refs/heads/feature", remote_sha)],
    )
    assert [step.name for step in plan] == ["smoke", "affected-tests"]
    assert "tests/v4/test_local_gate.py" in plan[1].argv


def test_a_new_remote_ref_uses_the_merge_base_with_the_base_branch(tmp_path):
    repo = repo_on_develop(tmp_path, {"tests/v4/test_git_hooks.py": "def test_x():\n    pass\n"})
    (repo / "tests/v4/test_git_hooks.py").write_text("def test_x():\n    assert True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "test: Update the hook test")
    local_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    plan = git_hooks.pre_push_plan(
        Context(repo, "develop"),
        [("refs/heads/new", local_sha, "refs/heads/new", "0" * 40)],
    )
    assert [step.name for step in plan] == ["smoke", "affected-tests"]
    assert "tests/v4/test_git_hooks.py" in plan[1].argv


def test_pre_push_main_reads_git_ref_records_from_stdin(tmp_path, monkeypatch):
    repo = repo_on_develop(tmp_path, {"tests/v4/test_local_gate.py": "def test_x():\n    pass\n"})
    remote_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "tests/v4/test_local_gate.py").write_text("def test_x():\n    assert True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "test: Update the test")
    local_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    seen = {}

    def capture(_ctx, plan, **_kwargs):
        seen["plan"] = plan
        return GateResult([StepResult("smoke", gate.PASSED)])

    monkeypatch.setattr(sys, "stdin", io.StringIO(
        f"refs/heads/feature {local_sha} refs/heads/feature {remote_sha}\n"))
    monkeypatch.setattr(gate, "run", capture)
    assert git_hooks.main(["pre-push", "--path", str(repo), "--push-stdin"]) == 0
    assert "tests/v4/test_local_gate.py" in seen["plan"][1].argv


def test_a_push_without_the_base_branch_is_refused_not_passed(tmp_path, capsys):
    git(tmp_path, "init", "-q", "-b", "develop")
    assert git_hooks.main(["pre-push", "--path", str(tmp_path)]) == 1
    assert "pre-push could not run" in capsys.readouterr().err


def test_the_pre_commit_checks_run_cheapest_first_and_the_guards_read_a_snapshot_of_the_index(tmp_path):
    repo = repo_on_develop(tmp_path)
    (repo / "aiflow/v4/core").mkdir(parents=True)
    (repo / "aiflow/v4/core/x.py").write_text("X = 1\n")
    git(repo, "add", ".")
    plan = git_hooks.pre_commit_plan(Context(repo, "develop"))
    assert [step.name for step in plan] == ["whitespace", "lint", "secrets", "giant-files", "duplicate-blocks",
                                             "broad-except", "sql-boundary"]
    assert plan[1].check is git_hooks._lint_staged
    snapshot = plan[3].argv[plan[3].argv.index("--head") + 1]
    assert git(repo, "cat-file", "-t", snapshot).stdout.strip() == "commit"
    assert snapshot != git(repo, "rev-parse", "HEAD").stdout.strip()


def _ruff_json(findings):
    return json.dumps([{"code": code, "message": message, "location": {"row": row, "column": 1}}
                       for row, code, message in findings])


def test_only_findings_on_changed_lines_refuse_a_commit(tmp_path, monkeypatch):
    repo = repo_on_develop(tmp_path)
    module = repo / "aiflow/v4/core/old.py"
    module.parent.mkdir(parents=True)
    module.write_text("import os\nVALUE = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "chore: Add old module with a finding")
    module.write_text("import os\nVALUE = 2\n")  # changes line 2 only; the old finding is on line 1
    git(repo, "add", "aiflow/v4/core/old.py")
    ctx = Context(repo, "develop")
    lint = next(step for step in git_hooks._commit_checks(ctx) if step.name == "lint")
    original_run = subprocess.run
    reported = []

    def fake_run(argv, *args, **kwargs):
        if argv[:3] == ["uv", "run", "--frozen"]:
            return subprocess.CompletedProcess(argv, 1, _ruff_json(reported), "")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(git_hooks.subprocess, "run", fake_run)
    reported[:] = [(1, "F401", "unused import")]
    assert lint.check(ctx) == []  # old finding on an untouched line
    reported[:] = [(1, "F401", "unused import"), (2, "E501", "line too long")]
    problems = lint.check(ctx)
    assert problems and "E501" in problems[0] and "F401" not in problems[0]
    git(repo, "reset", "-q")
    new = repo / "aiflow/v4/core/new.py"
    new.write_text("import sys\n")
    git(repo, "add", "aiflow/v4/core/new.py")
    reported[:] = [(1, "F401", "unused import")]
    assert lint.check(ctx) and "new.py" in lint.check(ctx)[0]  # a new file counts every line


def test_ruff_lints_the_staged_blob_instead_of_the_working_tree(tmp_path, monkeypatch):
    repo = repo_on_develop(tmp_path)
    module = repo / "aiflow/v4/core/x.py"
    module.parent.mkdir(parents=True)
    module.write_text("import os\n")  # staged F401
    git(repo, "add", "aiflow/v4/core/x.py")
    module.write_text("VALUE = 1\n")  # unstaged fix cannot hide it
    ctx = Context(repo, "develop")
    lint = next(step for step in git_hooks._commit_checks(ctx) if step.name == "lint")
    original_run = subprocess.run
    checked_sources = []

    def fake_run(argv, *args, **kwargs):
        if argv[:3] == ["uv", "run", "--frozen"]:
            source = kwargs["input"]
            checked_sources.append((argv, source))
            failing = "import os" in source
            return subprocess.CompletedProcess(argv, int(failing), _ruff_json([(1, "F401", "unused import")]) if failing else "[]", "")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(git_hooks.subprocess, "run", fake_run)
    staged_problems = lint.check(ctx)
    assert staged_problems and "F401" in staged_problems[0]
    assert checked_sources[-1][1] == "import os\n"
    assert "--stdin-filename" in checked_sources[-1][0] and checked_sources[-1][0][-1] == "-"

    module.write_text("VALUE = 1\n")
    git(repo, "add", "aiflow/v4/core/x.py")
    module.write_text("import os\n")  # unstaged violation cannot block the clean index
    assert lint.check(ctx) == []
    assert checked_sources[-1][1] == "VALUE = 1\n"


def test_duplicate_and_sql_guards_check_the_staged_tree_and_its_baselines(tmp_path):
    repo = repo_on_develop(tmp_path)
    first = repo / "aiflow/v4/core/first.py"
    second = repo / "aiflow/v4/core/second.py"
    first.parent.mkdir(parents=True)
    duplicated = "\n".join(f"STAGED_GUARD_MARKER_{i} = {i}" for i in range(8)) + "\n"
    distinct = duplicated.replace("STAGED_GUARD_MARKER_4 = 4", "STAGED_GUARD_MARKER_4 = 40") + "DIFFERENT = True\n"
    first.write_text(duplicated + 'QUERY = "SELECT id FROM staged_guard_table"\n')
    second.write_text(duplicated)
    git(repo, "add", "aiflow/v4/core/first.py", "aiflow/v4/core/second.py")
    first.write_text(duplicated)
    second.write_text(distinct)  # unstaged fixes cannot hide staged violations

    ctx = Context(repo, "develop")
    plan = git_hooks._commit_checks(ctx)
    duplicate = next(step for step in plan if step.name == "duplicate-blocks")
    sql = next(step for step in plan if step.name == "sql-boundary")
    duplicate_problems = duplicate.check(ctx)
    sql_problems = sql.check(ctx)
    assert duplicate_problems and "new fingerprint" in duplicate_problems[0]
    assert sql_problems and "no baseline entry" in sql_problems[0]

    first.write_text(duplicated)
    second.write_text(distinct)
    git(repo, "add", "aiflow/v4/core/first.py", "aiflow/v4/core/second.py")
    first.write_text(duplicated + 'QUERY = "SELECT id FROM staged_guard_table"\n')
    second.write_text(duplicated)  # unstaged violations cannot block a clean index
    clean_plan = git_hooks._commit_checks(ctx)
    duplicate = next(step for step in clean_plan if step.name == "duplicate-blocks")
    sql = next(step for step in clean_plan if step.name == "sql-boundary")
    assert duplicate.check(ctx) == []
    assert sql.check(ctx) == []


def test_a_skipped_check_is_left_out_and_named_in_the_output(tmp_path, monkeypatch, capsys):
    repo = repo_on_develop(tmp_path)
    monkeypatch.setenv(git_hooks.SKIP_VARIABLE, "secrets, sql-boundary")
    names = [step.name for step in git_hooks.pre_commit_plan(Context(repo, "develop"))]
    assert "secrets" not in names and "sql-boundary" not in names and "whitespace" in names
    assert "not run (AIFLOW_HOOK_SKIP): secrets, sql-boundary" in capsys.readouterr().err


def test_a_missing_gitleaks_refuses_the_commit_with_the_install_hint(tmp_path, monkeypatch):
    repo = repo_on_develop(tmp_path)
    monkeypatch.setattr(git_hooks.shutil, "which", lambda name: None)
    secrets = next(s for s in git_hooks.pre_commit_plan(Context(repo, "develop")) if s.name == "secrets")
    result = gate.run_step(secrets, Context(repo, "develop"), env={})
    assert result.status == gate.FAILED and "gitleaks is not installed" in result.tail


def test_a_missing_gitleaks_allowlist_refuses_the_commit_with_its_path(tmp_path):
    repo = repo_on_develop(tmp_path)
    (repo / GITLEAKS_CONFIG).unlink()
    secrets = git_hooks._secret_step(Context(repo, "develop"))
    result = gate.run_step(secrets, Context(repo, "develop"), env={})
    assert result.status == gate.FAILED and GITLEAKS_CONFIG in result.tail


def test_the_whitespace_check_refuses_a_commit_in_a_fresh_worktree_with_a_short_message(tmp_path, monkeypatch):
    monkeypatch.setenv(git_hooks.SKIP_VARIABLE, "whitespace,secrets")
    main = tmp_path / "main"
    main.mkdir()
    repo = repo_on_develop(main)
    install_hook_files(repo)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "chore: Add the hooks")
    assert gate.install_hooks(repo) == 0  # the setting is shared by the worktree that is created next
    worktree = tmp_path / "fresh"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "notes.txt").write_text("trailing spaces here   \n")
    git(worktree, "add", "notes.txt")
    env = hook_env(**IDENTITY, PYTHONPATH=str(ROOT))
    refused = subprocess.run(["git", "commit", "-q", "-m", "docs: Add notes (JAIF-1)"], cwd=worktree,
                             capture_output=True, text=True, env=env)
    report = refused.stdout + refused.stderr
    assert refused.returncode != 0 and "trailing whitespace" in report and "refused" in report
    assert len(report.splitlines()) <= git_hooks.SCREEN_LINES + 12


def test_install_makes_the_hooks_executable_because_git_skips_a_hook_that_is_not(tmp_path, capsys):
    git(tmp_path, "init", "-q", "-b", "develop")
    install_hook_files(tmp_path)
    hook = tmp_path / ".githooks" / "pre-commit"
    hook.chmod(0o644)
    assert gate.install_hooks(tmp_path) == 0
    assert os.access(hook, os.X_OK)
    assert "pre-commit (fast checks)" in capsys.readouterr().out
    assert git(tmp_path, "config", "core.hooksPath").stdout.strip() == ".githooks"


def test_install_repairs_hook_permissions_in_each_linked_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    repo = repo_on_develop(main)
    install_hook_files(repo)
    git(repo, "add", ".githooks")
    git(repo, "commit", "-q", "--no-verify", "-m", "chore: Add hook files")
    assert gate.install_hooks(repo) == 0

    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "linked", str(linked))
    hook = linked / ".githooks" / "pre-commit"
    hook.chmod(0o644)
    assert gate.install_hooks(linked) == 0
    assert os.access(hook, os.X_OK)


def test_install_refuses_missing_or_unreadable_hooks_before_setting_the_path(tmp_path, monkeypatch, capsys):
    git(tmp_path, "init", "-q", "-b", "develop")
    install_hook_files(tmp_path)
    (tmp_path / ".githooks" / "commit-msg").unlink()
    assert gate.install_hooks(tmp_path) != 0
    assert "commit-msg" in capsys.readouterr().err
    assert subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=tmp_path,
                          capture_output=True).returncode != 0

    install_hook_files(tmp_path)
    original_access = gate.os.access

    def deny_read(path, mode):
        if Path(path).name == "pre-push" and mode == os.R_OK:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(gate.os, "access", deny_read)
    assert gate.install_hooks(tmp_path) != 0
    assert "pre-push" in capsys.readouterr().err
    assert subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=tmp_path,
                          capture_output=True).returncode != 0


def test_install_warns_about_and_requires_force_for_an_existing_hooks_path(tmp_path, capsys):
    git(tmp_path, "init", "-q", "-b", "develop")
    install_hook_files(tmp_path)
    git(tmp_path, "config", "core.hooksPath", "custom-hooks")
    assert gate.main(["install-hooks", "--path", str(tmp_path)]) != 0
    warning = capsys.readouterr().err
    assert "custom-hooks" in warning and "--force" in warning
    assert git(tmp_path, "config", "core.hooksPath").stdout.strip() == "custom-hooks"

    assert gate.main(["install-hooks", "--path", str(tmp_path), "--force"]) == 0
    assert git(tmp_path, "config", "core.hooksPath").stdout.strip() == ".githooks"


def test_the_hook_scripts_hand_over_to_the_module():
    assert "aiflow.v4.core.git_hooks pre-commit" in (ROOT / ".githooks/pre-commit").read_text()
    assert "aiflow.v4.core.git_hooks pre-push" in (ROOT / ".githooks/pre-push").read_text()
    assert "--push-stdin" in (ROOT / ".githooks/pre-push").read_text()


def test_a_long_failure_is_cut_to_the_last_lines_of_one_screen():
    tail = "\n".join(f"line {i}" for i in range(200))
    result = GateResult([StepResult("lint", gate.FAILED, 1.0, tail)])
    lines = git_hooks.short_report(result).splitlines()
    assert lines[0].startswith("failed") and "line 199" in lines[-1] and "line 0" not in lines
    assert len(lines) <= git_hooks.SCREEN_LINES + 3
