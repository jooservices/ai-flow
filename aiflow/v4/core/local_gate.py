"""The local gate: the checks of the CI jobs, run before AI Flow commits and pushes (JAIF-365).

    python3 -m aiflow.v4.core.local_gate run --path <worktree> --base develop [--title "<pull request title>"]

The steps are the commands of the CI jobs ``Test (Unit)``, ``Giant files``, ``Documentation`` and ``Test (v4)`` plus the
header rules of the commit and the pull request title. ``tests/v4/test_local_gate.py`` compares them with
``.github/workflows/ci.yml`` so the gate cannot drift away from CI.

Exit code 0: every step passed. 1: a step failed. 2: nothing failed, but a step did not finish (time slice) or needs a
service or variable that is not provided, so CI has to complete it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import commit_rules

PASSED, FAILED, TIMEOUT, MISSING_ENV = "passed", "failed", "timeout", "missing_env"
TEST_SERVICES = ("AIFLOW4_TEST_REDIS_URL", "AIFLOW4_TEST_DATABASE_URL")
TAIL_LINES = 40
OUT_DIR = "<OUT>"  # build output goes to a temporary directory, never into the worktree that is about to be committed


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...] = ()
    ci_job: str = ""
    timeout_s: int = 600
    needs_env: tuple[str, ...] = ()
    check: Callable[["Context"], list[str]] | None = None  # in-process check: returns the problems found
    needs_commit: bool = False  # compares the committed HEAD with the base, so it cannot see uncommitted work
    retries: int = 0  # a step whose budget depends on machine speed (scale proof) is run once more before it fails


@dataclass(frozen=True)
class Context:
    path: Path
    base: str
    title: str = ""
    run_as: tuple[str, ...] = ()  # command prefix that runs the steps as the unprivileged AI user


@dataclass
class StepResult:
    name: str
    status: str
    seconds: float = 0.0
    tail: str = ""


@dataclass
class GateResult:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def failed(self) -> StepResult | None:
        return next((s for s in self.steps if s.status == FAILED), None)

    @property
    def incomplete(self) -> list[StepResult]:
        return [s for s in self.steps if s.status in (TIMEOUT, MISSING_ENV)]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 2 if self.incomplete else 0


def _commit_headers(ctx: Context) -> list[str]:
    out = subprocess.run([*ctx.run_as, "git", "log", f"origin/{ctx.base}..HEAD", "--format=%s"], cwd=ctx.path, text=True,
                         capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"cannot read the commits ahead of origin/{ctx.base}: {out.stderr.strip()[-200:]}")
    return [line for line in out.stdout.splitlines() if line.strip() and not line.startswith("Merge ")]


def check_headers(ctx: Context) -> list[str]:
    """Rule violations of the commit headers on the branch and of the pull request title, when one is given."""
    try:
        headers = _commit_headers(ctx)
    except RuntimeError as exc:
        return [str(exc)]  # an unreadable range is a failure, never a pass
    problems = [f"commit '{h}': {v}" for h in headers for v in commit_rules.violations(h)]
    if ctx.title:
        problems += [f"pull request title '{ctx.title}': {v}" for v in commit_rules.violations(ctx.title, pr_title=True)]
    return problems


def steps(base: str) -> list[Step]:
    """The gate steps in CI order. ``origin/<base>`` is the baseline the CI jobs use as ``$BASE``."""
    ref = f"origin/{base}"
    return [
        Step("compile", ("python3", "-m", "compileall", "-q", "aiflow"), "test", 300),
        Step("unit", ("python3", "-m", "unittest", "discover", "-s", "tests"), "test", 600),
        Step("giant-files", ("python3", "-m", "aiflow.v4.core.giant_files", "--base", ref, "--head", "HEAD"),
             "giant-files", 300, needs_commit=True),
        Step("docs-build", ("uv", "run", "--frozen", "python", "-m", "aiflow.v4.core.docsite", "build", "--out",
                            OUT_DIR), "docs", 600),
        Step("docs-check", ("uv", "run", "--frozen", "python", "-m", "aiflow.v4.core.docsite", "check", "--base", ref),
             "docs", 300, needs_commit=True),
        Step("headers", check=check_headers, needs_commit=True),
        Step("scale-proof", ("uv", "run", "--frozen", "pytest", "tests/v4/test_scale_proof.py", "-p", "no:warnings"),
             "v4", 900, TEST_SERVICES, retries=1),
        Step("v4", ("uv", "run", "--frozen", "pytest", "tests/v4", "--ignore=tests/v4/test_scale_proof.py",
                    "-p", "no:warnings"), "v4", 2400, TEST_SERVICES),
    ]


def normalize(argv: tuple[str, ...] | list[str], base: str = "") -> str:
    """A command in a form that ignores what differs between CI and a worktree (``--frozen``, ``-v``, the base)."""
    tokens = [t for t in argv if t not in ("--frozen", "-v")]
    tokens = [OUT_DIR if i and tokens[i - 1] == "--out" else t for i, t in enumerate(tokens)]
    if base:
        tokens = ["<BASE>" if t == f"origin/{base}" else t for t in tokens]
    return " ".join(tokens)


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-TAIL_LINES:])


def _read_tail(path: Path, limit: int = 65536) -> str:
    """The last bytes of a step's output file, decoded leniently."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_command(step: Step, ctx: Context, env: dict[str, str]) -> StepResult:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aiflow-gate-") as scratch:
        os.chmod(scratch, 0o777)  # a step may run as the unprivileged AI user
        argv = [str(Path(scratch) / "out") if t == OUT_DIR else t for t in step.argv]
        log_path = Path(scratch) / "output.log"
        with open(log_path, "wb") as sink:  # the output goes to a file, so a chatty check cannot fill the worker's memory
            os.chmod(log_path, 0o666)
            proc = subprocess.Popen([*ctx.run_as, *argv], cwd=ctx.path, env=env, stdout=sink, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                proc.wait(timeout=step.timeout_s)
            except subprocess.TimeoutExpired:
                _kill(proc)  # the whole process group, so a detached child cannot keep the gate waiting
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return StepResult(step.name, TIMEOUT, time.monotonic() - started,
                                  f"no result after {step.timeout_s} s\n{_tail(_read_tail(log_path))}")
        return StepResult(step.name, PASSED if proc.returncode == 0 else FAILED, time.monotonic() - started,
                          "" if proc.returncode == 0 else _tail(_read_tail(log_path)))


def run_step(step: Step, ctx: Context, env: dict[str, str] | None = None) -> StepResult:
    env = dict(os.environ if env is None else env)
    missing = [name for name in step.needs_env if not env.get(name)]
    if missing:
        return StepResult(step.name, MISSING_ENV, 0.0, "not provided: " + ", ".join(missing))
    if step.check:
        started = time.monotonic()
        problems = step.check(ctx)
        return StepResult(step.name, FAILED if problems else PASSED, time.monotonic() - started, "\n".join(problems))
    result = run_command(step, ctx, env)
    for _ in range(step.retries):
        if result.status != FAILED:
            break
        result = run_command(step, ctx, env)
    return result


def run(ctx: Context, plan: list[Step] | None = None, env: dict[str, str] | None = None,
        only: set[str] | None = None, worktree_only: bool = False,
        log: Callable[[str], None] = lambda line: None) -> GateResult:
    """Run the steps in order and stop at the first failure (a step that did not finish does not stop the gate)."""
    result = GateResult()
    for step in plan if plan is not None else steps(ctx.base):
        if (only and step.name not in only) or (worktree_only and step.needs_commit):
            continue
        log(f"gate: {step.name} ...")
        got = run_step(step, ctx, env)
        result.steps.append(got)
        log(f"gate: {step.name} {got.status} in {got.seconds:.0f} s")
        if got.status == FAILED:
            break
    return result


def ci_commands(workflow_text: str, jobs: tuple[str, ...]) -> list[str]:
    """The check commands (``python3 -m ...`` and ``uv run ...``) of the given CI jobs, without comments."""
    import yaml
    data = yaml.safe_load(workflow_text)
    found: list[str] = []
    for job in jobs:
        for item in (data["jobs"][job].get("steps") or []):
            for line in str(item.get("run") or "").splitlines():
                line = line.strip()
                if line.startswith(("python3 -m", "uv run")):
                    found.append(" ".join(shlex.split(line, comments=True)))
    return found


def render(result: GateResult) -> str:
    lines = [f"{s.status:<11} {s.name} ({s.seconds:.0f} s)" for s in result.steps]
    failed = result.failed
    if failed:
        lines += ["", f"--- {failed.name} failed ---", failed.tail]
    for s in result.incomplete:
        lines += ["", f"--- {s.name}: {s.status} ---", s.tail]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local_gate", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="run the gate")
    runner.add_argument("--path", default=".", help="worktree to check")
    runner.add_argument("--base", default="develop", help="base branch of the pull request")
    runner.add_argument("--title", default="", help="pull request title to check as well")
    runner.add_argument("--only", default="", help="comma separated step names")
    runner.add_argument("--worktree", action="store_true",
                        help="skip the steps that compare the committed HEAD with the base (use before committing)")
    runner.add_argument("--json", action="store_true", help="print the result as JSON")
    sub.add_parser("steps", help="list the steps")
    hooks = sub.add_parser("install-hooks", help="use .githooks (commit message rules, gate before push) in this clone")
    hooks.add_argument("--path", default=".")
    args = parser.parse_args(argv)
    if args.command == "install-hooks":
        out = subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=args.path, capture_output=True, text=True)
        print(out.stderr.strip() or "git hooks installed: commit-msg (title rules) and pre-push (CI checks)")
        return out.returncode
    if args.command == "steps":
        for step in steps("<base>"):
            print(f"{step.name:<12} {step.ci_job or '-':<12} {normalize(step.argv) or '(in-process check)'}")
        return 0
    ctx = Context(Path(args.path).resolve(), args.base, args.title)
    only = {n for n in args.only.split(",") if n} or None
    unknown = sorted((only or set()) - {s.name for s in steps(args.base)})
    if unknown:
        print("unknown step(s): " + ", ".join(unknown) + "; run `local_gate steps` for the names", file=sys.stderr)
        return 2
    result = run(ctx, only=only, worktree_only=args.worktree, log=lambda line: print(line, file=sys.stderr, flush=True))
    if args.json:
        print(json.dumps({"exit": result.exit_code, "steps": [vars(s) for s in result.steps]}))
    else:
        print(render(result))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
