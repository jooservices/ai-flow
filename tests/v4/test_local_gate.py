"""The local gate (JAIF-365): parity with CI, step results, and the developer run's use of it."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aiflow.v4.core import local_gate as gate
from aiflow.v4.core.local_gate import Context, Step
from aiflow.v4.worker import gate_step

ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def normalized_ci(jobs):
    out = set()
    for command in gate.ci_commands(CI, jobs):
        tokens = ["<BASE>" if t in ('"$BASE"', "$BASE") else t for t in command.split()]
        out.add(gate.normalize(tokens))
    return out


def test_every_gated_ci_command_is_a_gate_step():
    """CI changes a check: the gate must change with it, or this test fails."""
    in_gate = {gate.normalize(s.argv, "develop") for s in gate.steps("develop") if s.argv}
    ci = normalized_ci(("test", "giant-files", "docs", "v4"))
    assert ci <= in_gate, f"CI runs commands the gate does not: {sorted(ci - in_gate)}"


def test_gate_steps_follow_the_ci_order_and_end_with_the_v4_suites():
    names = [s.name for s in gate.steps("develop")]
    assert names[:2] == ["compile", "unit"] and names[-2:] == ["scale-proof", "v4"]
    assert names.index("scale-proof") < names.index("v4")  # the benchmark runs first, as in CI


def ctx(tmp_path):
    return Context(tmp_path, "develop")


def py(code):
    return (sys.executable, "-c", code)


def test_passed_failed_and_stop_at_the_first_failure(tmp_path):
    plan = [Step("ok", py("print(1)")), Step("bad", py("print('boom line'); raise SystemExit(3)")),
            Step("never", py("print(2)"))]
    result = gate.run(ctx(tmp_path), plan)
    assert [s.name for s in result.steps] == ["ok", "bad"]
    assert result.failed.name == "bad" and "boom line" in result.failed.tail
    assert result.exit_code == 1


def test_a_step_that_exceeds_its_time_slice_is_reported_not_finished(tmp_path):
    plan = [Step("slow", py("import time; time.sleep(30)"), timeout_s=1), Step("after", py("print(1)"))]
    result = gate.run(ctx(tmp_path), plan)
    assert [s.status for s in result.steps] == [gate.TIMEOUT, gate.PASSED]
    assert result.failed is None and result.exit_code == 2


def test_a_missing_service_variable_is_a_missing_environment_item_not_a_failure(tmp_path):
    plan = [Step("needs", py("print(1)"), needs_env=("AIFLOW_GATE_TEST_ONLY",))]
    result = gate.run(ctx(tmp_path), plan, env={"PATH": "/usr/bin"})
    assert result.steps[0].status == gate.MISSING_ENV and result.exit_code == 2
    assert "AIFLOW_GATE_TEST_ONLY" in result.steps[0].tail


def test_everything_passing_exits_zero_and_only_selects_steps(tmp_path):
    plan = [Step("a", py("print(1)")), Step("b", py("print(2)")), Step("c", py("print(3)"), needs_commit=True)]
    assert gate.run(ctx(tmp_path), plan, only={"b"}).steps[0].name == "b"
    assert [s.name for s in gate.run(ctx(tmp_path), plan, worktree_only=True).steps] == ["a", "b"]
    assert gate.run(ctx(tmp_path), plan).exit_code == 0


def git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(path),
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def test_header_step_checks_commits_ahead_of_the_base_and_the_title(tmp_path):
    git(tmp_path, "init", "-q", "-b", "develop")
    (tmp_path / "f").write_text("1")
    git(tmp_path, "add", "."); git(tmp_path, "commit", "-q", "-m", "chore: Start the repository")
    git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    (tmp_path / "f").write_text("2")
    git(tmp_path, "add", "."); git(tmp_path, "commit", "-q", "-m", "fix: lower case subject")
    problems = gate.check_headers(Context(tmp_path, "develop", "feat: Ok title here"))
    assert any("lower-case" in p for p in problems) and len(problems) == 1
    git(tmp_path, "commit", "-q", "--amend", "-m", "fix: Upper case subject")
    assert gate.check_headers(Context(tmp_path, "develop", "feat: Fine title (K-1)")) == []
    assert gate.check_headers(Context(tmp_path, "develop", "feat: bad title here"))


def test_project_opt_in_prompt_line_and_custom_commands():
    assert not gate_step.enabled({}) and gate_step.enabled({"local_gate": "ai-flow"})
    line = gate_step.prompt_check({"local_gate": "ai-flow", "base": "develop"})
    assert "local_gate run --path . --base develop --worktree" in line
    assert gate_step.prompt_check({"local_gate": ["make test"]}) == ""
    custom = gate_step.plan({"local_gate": ["make test", "make lint"]})
    assert [s.argv for s in custom] == [("make", "test"), ("make", "lint")]
    assert [s.name for s in gate_step.plan({"local_gate": "ai-flow"})] == [s.name for s in gate.steps("develop")]


def test_dev_prompt_lists_the_gate_command_in_the_checks():
    from types import SimpleNamespace
    from aiflow.v4.worker import dev
    c = SimpleNamespace(project={"checks": ["pytest"], "base": "develop", "local_gate": "ai-flow", "repo": "o/r"},
                        ticket={"key": "K-1", "project": "K", "summary": "s", "description": "d"}, findings="",
                        triage_answer="", triage_question="", related=[], machine_tools={}, context_pack=None)
    prompt = dev.build_prompt(c, "K-1-branch", None, "", {})
    assert "aiflow.v4.core.local_gate run --path . --base develop --worktree" in prompt


def test_failure_and_pull_request_notes():
    failed = gate.GateResult([gate.StepResult("v4", gate.FAILED, 1, "E assert 1 == 2")])
    assert "step v4 failed" in gate_step.failure_text(failed) and "assert 1 == 2" in gate_step.failure_text(failed)
    partial = gate.GateResult([gate.StepResult("v4", gate.TIMEOUT, 9, "no result after 2400 s"),
                               gate.StepResult("scale-proof", gate.MISSING_ENV, 0, "not provided: X")])
    note = gate_step.pr_note(partial)
    assert note.startswith("## Local gate") and "v4: timeout" in note and "scale-proof: missing_env" in note
    assert gate_step.pr_note(gate.GateResult([gate.StepResult("v4", gate.PASSED)])) == ""


def test_docs_build_writes_outside_the_worktree(tmp_path):
    step = next(s for s in gate.steps("develop") if s.name == "docs-build")
    assert gate.OUT_DIR in step.argv and "build/docs-site" not in step.argv
    probe = Step("probe", py("import sys; sys.exit(3 if '<' in sys.argv[1] else 0)") + ("<OUT>",))
    result = gate.run_command(probe, ctx(tmp_path), {})
    assert result.status == gate.PASSED


def hook_repo(tmp_path):
    """A scratch repository that uses this repository's hooks and code."""
    git(tmp_path, "init", "-q", "-b", "develop")
    subprocess.run(["git", "config", "core.hooksPath", str(ROOT / ".githooks")], cwd=tmp_path, check=True)
    (tmp_path / "f").write_text("1")
    git(tmp_path, "add", ".")
    return tmp_path


def commit(path, message):
    import os
    env = {**os.environ, "PYTHONPATH": str(ROOT), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, capture_output=True, text=True, env=env)


def test_the_commit_msg_hook_rejects_a_bad_message_when_committing(tmp_path):
    repo = hook_repo(tmp_path)
    bad = commit(repo, "fix: lower case subject")
    assert bad.returncode != 0 and "Commit message rejected" in bad.stdout + bad.stderr
    assert "lower-case" in bad.stdout + bad.stderr
    good = commit(repo, "fix: Upper case subject (JAIF-1)")
    assert good.returncode == 0, good.stderr


def test_the_commit_msg_hook_lets_merge_and_fixup_headers_through(tmp_path):
    from aiflow.v4.core import commit_rules
    for header in ("Merge branch 'develop' into x", 'Revert "feat: Add a thing"', "fixup! feat: Add a thing"):
        message = tmp_path / "MSG"
        message.write_text(header + "\n\n# comment line\n")
        assert commit_rules.check_message_file(str(message)) == 0


def test_install_hooks_points_the_clone_at_the_hooks_folder(tmp_path):
    git(tmp_path, "init", "-q", "-b", "develop")
    assert gate.main(["install-hooks", "--path", str(tmp_path)]) == 0
    out = subprocess.run(["git", "config", "core.hooksPath"], cwd=tmp_path, capture_output=True, text=True)
    assert out.stdout.strip() == ".githooks"


def test_hooks_are_executable_files_of_the_repository():
    import os
    for name in ("commit-msg", "pre-push"):
        path = ROOT / ".githooks" / name
        assert path.is_file() and os.access(path, os.X_OK)


def test_a_load_sensitive_step_is_run_once_more_before_it_fails(tmp_path):
    flag = tmp_path / "first-run-done"
    code = f"import pathlib,sys; f=pathlib.Path({str(flag)!r}); ok=f.exists(); f.write_text('x'); sys.exit(0 if ok else 3)"
    flaky = Step("flaky", py(code), retries=1)
    assert gate.run(ctx(tmp_path), [flaky]).exit_code == 0  # fails once, passes on the retry
    assert gate.run(ctx(tmp_path), [Step("always", py("raise SystemExit(3)"), retries=1)]).exit_code == 1
    assert next(s for s in gate.steps("develop") if s.name == "scale-proof").retries == 1


def test_an_unreadable_commit_range_fails_the_headers_step(tmp_path):
    git(tmp_path, "init", "-q", "-b", "develop")  # no origin/develop: `git log origin/develop..HEAD` fails
    problems = gate.check_headers(Context(tmp_path, "develop"))
    assert problems and "cannot read the commits ahead of origin/develop" in problems[0]


def test_a_detached_child_holding_the_pipe_does_not_hang_the_gate(tmp_path):
    code = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            " start_new_session=True); import time; time.sleep(30)")
    started = __import__("time").monotonic()
    result = gate.run_command(Step("hang", py(code), timeout_s=1), ctx(tmp_path), {})
    assert result.status == gate.TIMEOUT and __import__("time").monotonic() - started < 15


def test_the_pre_push_hook_refuses_a_ref_that_is_not_the_checked_out_commit(tmp_path):
    import os
    repo = hook_repo(tmp_path)
    commit(repo, "fix: A valid first commit (JAIF-1)")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    hook = ROOT / ".githooks" / "pre-push"
    other = subprocess.run([str(hook)], cwd=repo, input="refs/heads/x " + "1" * 40 + " refs/heads/x " + "0" * 40 + "\n",
                           text=True, capture_output=True, env=env)
    assert other.returncode == 1 and "check that branch out first" in other.stderr


def test_a_project_with_an_unsupported_local_gate_is_rejected_at_load():
    import pytest as _pytest
    from aiflow.v4.core.controlrepo import Project
    for bad in ("make test", "ai-flwo", [], [1], ["ok", ""]):
        with _pytest.raises(ValueError, match="local_gate must be"):
            Project(key="K", repo="o/r", local_gate=bad)
    assert Project(key="K", repo="o/r", local_gate="ai-flow").local_gate == "ai-flow"
    assert Project(key="K", repo="o/r", local_gate=["make test"]).local_gate == ["make test"]


def test_unknown_step_names_in_only_are_rejected_not_skipped(tmp_path, capsys):
    assert gate.main(["run", "--path", str(tmp_path), "--only", "v-4"]) == 2
    assert "unknown step(s): v-4" in capsys.readouterr().err


def test_the_worker_gate_environment_never_carries_the_workers_secrets(monkeypatch):
    monkeypatch.setenv("AIFLOW4_SECRET_KEY", "worker-secret")
    monkeypatch.setenv("GH_TOKEN", "another-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = gate_step.gate_env({"AIFLOW4_TEST_REDIS_URL": "redis://x"})
    assert env["PATH"] == "/usr/bin" and env["AIFLOW4_TEST_REDIS_URL"] == "redis://x"
    assert "AIFLOW4_SECRET_KEY" not in env and "GH_TOKEN" not in env


def test_a_chatty_step_is_read_from_a_file_and_only_its_tail_is_kept(tmp_path):
    code = "import sys\nfor i in range(200000):\n    print('line', i)\nsys.exit(3)"
    result = gate.run_command(Step("chatty", py(code)), ctx(tmp_path), {})
    assert result.status == gate.FAILED and "line 199999" in result.tail and "line 5\n" not in result.tail
    assert len(result.tail.splitlines()) <= gate.TAIL_LINES


def test_the_pre_push_hook_refuses_a_dirty_working_tree(tmp_path):
    import os
    repo = hook_repo(tmp_path)
    commit(repo, "fix: A valid first commit (JAIF-1)")
    (repo / "f").write_text("changed")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    out = subprocess.run([str(ROOT / ".githooks" / "pre-push")], cwd=repo, input=f"refs/heads/x {head} refs/heads/x {'0' * 40}\n",
                         text=True, capture_output=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 1 and "uncommitted changes" in out.stderr


def test_a_step_never_inherits_the_repository_variables_a_git_hook_sets(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/index")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Kept")
    probe = Step("probe", py("import os, sys; sys.exit(3 if 'GIT_DIR' in os.environ or 'GIT_INDEX_FILE' in os.environ "
                             "or os.environ.get('GIT_AUTHOR_NAME') != 'Kept' else 0)"))
    assert gate.run_step(probe, ctx(tmp_path)).status == gate.PASSED
