"""Run and record the checks that must pass before a developer run can be submitted for review."""
from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Callable

from ..core import local_gate
from ..core.local_gate import Context, GateResult, Step
from . import suite_slots

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
        return local_gate.run(ctx, plan(project), gate_env(env), log=log)


def keep_failed_work(git, wt: Path, base: Path, commits: list[str] | None = None) -> str:
    """Save run-authored commits and remaining edits without copying a base-refresh merge into the patch."""
    path = base.with_suffix(".gate-failed.patch")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        patches = []
        for commit in dict.fromkeys(commits or ()):
            parent = git.run("rev-parse", f"{commit}^", cwd=wt).strip()
            patches.append(git.run("diff", "--binary", parent, commit, cwd=wt))
        working = git.run("diff", "--binary", "HEAD", cwd=wt)
        if working:
            patches.append(working)
        if not any(patches):
            patches.append(git.run("diff", "--binary", "HEAD~1", "HEAD", cwd=wt))
        path.write_text("\n".join(patch for patch in patches if patch), encoding="utf-8")
    except Exception:  # noqa: BLE001 - saving is best effort; the gate result is what matters
        return "(not saved)"
    return str(path)


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
