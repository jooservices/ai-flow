"""The developer run's use of the local gate: the same checks as CI before anything is pushed (JAIF-365).

A project opts in with ``local_gate`` in its control-repo file: ``ai-flow`` (the built-in steps of
``core/local_gate.py``) or a list of commands. AI Flow commits locally, runs the gate on that commit and pushes only
when no step failed; a step that did not finish (time slice) or lacks a service is stated in the pull request so CI
completes it.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Callable

from ..core import local_gate
from ..core.local_gate import Context, GateResult, Step

CUSTOM_TIMEOUT_S = 1800


def enabled(project: dict) -> bool:
    return bool(project.get("local_gate"))


def plan(project: dict) -> list[Step]:
    setting = project.get("local_gate")
    if isinstance(setting, (list, tuple)):
        return [Step(f"check-{i}", tuple(shlex.split(str(cmd))), "", CUSTOM_TIMEOUT_S) for i, cmd in enumerate(setting, 1)]
    return local_gate.steps(project.get("base") or "develop")


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
    ctx = Context(wt, project.get("base") or "develop", title, tuple(run_as or ()))
    return local_gate.run(ctx, plan(project), gate_env(env), log=log)


def keep_failed_work(git, wt: Path, base: Path) -> str:
    """Save the commit that failed the gate as a patch next to the run log, because the next claim cleans the worktree."""
    path = base.with_suffix(".gate-failed.patch")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(git.run("diff", "--binary", "HEAD~1", "HEAD", cwd=wt), encoding="utf-8")
    except Exception:  # noqa: BLE001 - saving is best effort; the gate result is what matters
        return "(not saved)"
    return str(path)


def failure_text(result: GateResult) -> str:
    step = result.failed
    return f"step {step.name} failed:\n{step.tail}" if step else ""


def pr_note(result: GateResult) -> str:
    """A pull request section for steps that could not finish locally (empty when everything passed)."""
    if not result.incomplete:
        return ""
    lines = [f"- {s.name}: {s.status}" + (f" ({s.tail.splitlines()[0]})" if s.tail else "") for s in result.incomplete]
    return "## Local gate\nNot finished locally, so CI completes these steps:\n" + "\n".join(lines) + "\n\n"
