"""Run and record the checks that must pass before a developer run can be submitted for review."""
from __future__ import annotations

import os
import shlex
import tempfile
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from ..core import local_gate
from ..core.local_gate import Context, GateResult, Step
from . import suite_slots
from .gitops import GitError

CUSTOM_TIMEOUT_S = 1800


def enabled(project: dict) -> bool:
    return bool(project.get("local_gate"))


def plan(project: dict) -> list[Step]:
    setting = project.get("local_gate")
    if isinstance(setting, (list, tuple)):
        return [Step(f"check-{i}", tuple(shlex.split(str(cmd))), "", CUSTOM_TIMEOUT_S) for i, cmd in enumerate(setting, 1)]
    if setting == "ai-flow":
        return local_gate.steps(project.get("base") or "develop")
    checks = project.get("checks") or []
    return [Step(f"check-{i}", tuple(shlex.split(str(cmd))), "", CUSTOM_TIMEOUT_S)
            for i, cmd in enumerate(checks, 1)]


def prompt_check(project: dict) -> str:
    """The check line for the developer prompt: the AI repeats the working-tree part of the gate until it passes."""
    if isinstance(project.get("local_gate"), (list, tuple)):
        return ""  # the project's own commands are already listed as checks
    return (f"python3 -m aiflow.v4.core.local_gate run --path . --base {project.get('base') or 'develop'} --worktree"
            " (the CI checks; it must pass before you finish. AI Flow runs the rest after it commits)")


BASE_ENV = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "HOME", "USER", "LOGNAME", "XDG_CACHE_HOME", "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR")


def gate_env(env: dict[str, str] | None) -> dict[str, str]:
    """Only the project's run environment plus the few variables a toolchain needs; never the worker's secrets."""
    return {**{k: os.environ[k] for k in BASE_ENV if k in os.environ}, **(env or {})}


def run(wt: Path, project: dict, title: str, env: dict[str, str] | None = None, run_as: list[str] | None = None,
        log: Callable[[str], None] = lambda line: None) -> GateResult:
    ctx = Context(wt, project.get("base") or "develop", title,
                  tuple(suite_slots.nice_prefix(run_as or [])))
    with suite_slots.suite_slot():
        with tempfile.TemporaryDirectory(prefix="aiflow4-gate-test-slots-") as slot_directory:
            nested_env = {**gate_env(env), "AIFLOW4_TEST_SLOT_DIR": slot_directory}
            return local_gate.run(ctx, plan(project), nested_env, log=log)


def keep_failed_work(git, wt: Path, base: Path, commits: list[str] | None = None,
                     repair_base: str = "") -> str:
    """Save run-authored commits and the complete repair delta without copying a base refresh into the patch."""
    path = base.with_suffix(".gate-failed.patch")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        patches = []
        for commit in dict.fromkeys(commits or ()):
            parent = git.run("rev-parse", f"{commit}^", cwd=wt).strip()
            patches.append(git.run("diff", "--binary", parent, commit, cwd=wt))
        if repair_base:
            index_path = git.run("rev-parse", "--git-path", "index", cwd=wt).strip()
            index_path = Path(index_path)
            if not index_path.is_absolute():
                index_path = wt / index_path
            repair_index = str(index_path.with_name(f"index.gate-failed-{uuid.uuid4().hex}"))
            try:
                _run_git_with_index(git, wt, repair_index, "read-tree", "HEAD")
                _run_git_with_index(git, wt, repair_index, "add", "-A")
                repair = _run_git_with_index(git, wt, repair_index, "diff", "--binary", "--cached", repair_base)
                if repair:
                    patches.append(repair)
            finally:
                Path(repair_index).unlink(missing_ok=True)
                Path(f"{repair_index}.lock").unlink(missing_ok=True)
        else:
            working = git.run("diff", "--binary", "HEAD", cwd=wt)
            if working:
                patches.append(working)
        if not any(patches):
            patches.append(git.run("diff", "--binary", "HEAD~1", "HEAD", cwd=wt))
        path.write_text("\n".join(patch for patch in patches if patch), encoding="utf-8")
    except Exception:  # noqa: BLE001 - saving is best effort; the gate result is what matters
        return "(not saved)"
    return str(path)


def snapshot_worktree(git, wt: Path) -> str:
    """Capture tracked and untracked worktree content as a tree without touching its real index."""
    index_path = git.run("rev-parse", "--git-path", "index", cwd=wt).strip()
    index_path = Path(index_path)
    if not index_path.is_absolute():
        index_path = wt / index_path
    snapshot_index = str(index_path.with_name(f"index.gate-repair-{uuid.uuid4().hex}"))
    try:
        _run_git_with_index(git, wt, snapshot_index, "read-tree", "HEAD")
        _run_git_with_index(git, wt, snapshot_index, "add", "-A")
        return _run_git_with_index(git, wt, snapshot_index, "write-tree").strip()
    finally:
        Path(snapshot_index).unlink(missing_ok=True)
        Path(f"{snapshot_index}.lock").unlink(missing_ok=True)


def changes_since_snapshot(git, wt: Path, before: str) -> list[str]:
    """Return only paths whose tracked or untracked content changed after a repair snapshot."""
    after = snapshot_worktree(git, wt)
    if before == after:
        return []
    output = git.run("diff", "--no-renames", "--name-only", before, after, cwd=wt)
    return [path for path in output.splitlines() if path]


def commit_repair(git, wt: Path, paths: list[str], message: str, identity: tuple[str, str]) -> str:
    """Commit the paths a repair changed, leaving unrelated gate artifacts out of the new commit."""
    if not paths:
        raise GitError("gate repair has no changed paths")
    git.run("add", "-A", "--", *paths, cwd=wt)
    result = subprocess.run(
        git.run_as + ["git", "commit", "--only", "-q", "-F", "-", "--", *paths], cwd=str(wt), input=message,
        text=True, capture_output=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                            "HOME": str(git.root / "cache"),
                                            "GIT_AUTHOR_NAME": identity[0], "GIT_AUTHOR_EMAIL": identity[1],
                                            "GIT_COMMITTER_NAME": identity[0], "GIT_COMMITTER_EMAIL": identity[1]})
    if result.returncode:
        raise GitError(f"git commit: {result.stderr.strip()[-400:]}")
    return git.run("rev-parse", "HEAD", cwd=wt).strip()


def _run_git_with_index(git, wt: Path, index_path: str, *args: str) -> str:
    """Run one Git command against a temporary index without changing the shared Git wrapper API."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(git.root / "cache"),
           "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_INDEX_FILE": index_path}
    result = subprocess.run(git.run_as + ["git", *args], cwd=str(wt), env=env, capture_output=True, text=True,
                            timeout=600)
    if result.returncode:
        raise GitError(f"git {' '.join(args[:3])}: {result.stderr.strip()[-400:]}")
    return result.stdout


def failure_text(result: GateResult) -> str:
    step = result.failed
    if step:
        return f"step {step.name} failed:\n{step.tail}"
    if result.incomplete:
        return "checks did not finish: " + ", ".join(f"{item.name} ({item.status})" for item in result.incomplete)
    return ""


def result_record(result: GateResult) -> dict:
    """Return the short, stable run-record fields used by verification dispatch and later metrics."""
    status = "green" if result.exit_code == 0 else "red" if result.failed else "incomplete"
    return {"status": status, "steps": [{"name": item.name, "status": item.status} for item in result.steps]}


def pr_note(result: GateResult) -> str:
    """A pull request section for steps that could not finish locally (empty when everything passed)."""
    if not result.incomplete:
        return ""
    lines = [f"- {s.name}: {s.status}" + (f" ({s.tail.splitlines()[0]})" if s.tail else "") for s in result.incomplete]
    return "## Local gate\nNot finished locally, so CI completes these steps:\n" + "\n".join(lines) + "\n\n"
