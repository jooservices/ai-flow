"""The git hooks of this repository (JAIF-581): fast checks when a commit is made, affected tests when a push is made.

    python3 -m aiflow.v4.core.git_hooks pre-commit [--base develop]
    python3 -m aiflow.v4.core.git_hooks pre-push [--base develop]

``pre-commit`` checks the staged change: whitespace errors, ruff lint of the staged v4 modules (findings on added or changed lines only), gitleaks, and the
project guards (giant files, duplicate blocks, broad except, SQL boundary). The guards that compare commits see the
staged change through a snapshot commit that no branch points at. The commit-message rules stay in the
``commit-msg`` hook.

``pre-push`` runs the v4 tests that the push changes, plus the architecture and feature-structure smoke tests.
No AI token is spent: the output is short. Exit codes as ``local_gate``: 0 passed, 1 refused, 2 a check did not run.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import local_gate as gate
from .local_gate import Context, GateResult, Step

PY = "python3"
UV_PYTEST = ("uv", "run", "--frozen", "pytest")
TESTS = Path("tests/v4")
SMOKE_TESTS = ("tests/v4/test_architecture.py", "tests/v4/test_feature_structure.py")
GITLEAKS_CONFIG = "deploy/v4/ci/gitleaks.toml"
SKIP_VARIABLE = "AIFLOW_HOOK_SKIP"  # comma separated check names to skip; the report names the skipped checks
SCREEN_LINES = 20  # the refusal fits one screen: the summary of the steps plus the last lines of the failing step
IDENTITY = {"GIT_AUTHOR_NAME": "ai-flow hooks", "GIT_AUTHOR_EMAIL": "hooks@ai-flow.invalid",
            "GIT_COMMITTER_NAME": "ai-flow hooks", "GIT_COMMITTER_EMAIL": "hooks@ai-flow.invalid"}


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()[-200:]}")
    return out.stdout.strip()


def staged_snapshot(root: Path) -> str:
    """A commit of the index that no branch points at: the commit-based guards read what is about to be committed."""
    tree = _git(root, "write-tree")
    has_head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root, capture_output=True).returncode == 0
    parents = ["-p", "HEAD"] if has_head else []
    return _git(root, "commit-tree", tree, *parents, "-m", "pre-commit snapshot",
                env={**os.environ, **IDENTITY})


def staged_python_modules(root: Path) -> list[str]:
    names = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    return [name for name in names if name.startswith("aiflow/v4/") and name.endswith(".py")]


def _changed_lines(root: Path, name: str) -> set[int] | None:
    """Line numbers a staged file adds or changes; ``None`` when the file is new (every line counts)."""
    if _git(root, "diff", "--cached", "--name-only", "--diff-filter=A", "--", name):
        return None
    lines: set[int] = set()
    for hunk in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", _git(root, "diff", "--cached", "-U0", "--", name),
                            re.M):
        first, count = int(hunk.group(1)), int(hunk.group(2) or 1)
        lines.update(range(first, first + count))
    return lines


def _new_findings(output: str, changed: set[int] | None) -> list[str]:
    """Ruff findings (JSON output) on changed lines; output that is not JSON is reported whole."""
    try:
        findings = json.loads(output)
    except ValueError:
        return [output.strip()] if output.strip() else []
    return [f"{item['location']['row']}:{item['location']['column']}: {item['code']} {item['message']}"
            for item in findings if changed is None or item["location"]["row"] in changed]


def _lint_staged(ctx: Context) -> list[str]:
    """Run Ruff on each Python module's staged blob, with its repository path for configuration. Only findings on
    lines the commit adds or changes refuse it, so a file with old findings can still be committed (JAIF-589)."""
    problems = []
    for name in staged_python_modules(ctx.path):
        shown = subprocess.run(["git", "show", f":{name}"], cwd=ctx.path, capture_output=True, text=True)
        if shown.returncode:
            problems.append(f"cannot read staged {name}: {shown.stderr.strip()[-200:]}")
            continue
        try:
            checked = subprocess.run(
                ["uv", "run", "--frozen", "ruff", "check", "--force-exclude", "--no-cache", "--output-format", "json",
                 "--stdin-filename", name, "-"],
                cwd=ctx.path, input=shown.stdout, capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"ruff timed out while checking staged {name}")
            continue
        if checked.returncode:
            found = _new_findings(checked.stdout, _changed_lines(ctx.path, name))
            if found or not checked.stdout.strip():
                detail = "\n".join(found) or checked.stderr.strip()
                problems.append(f"ruff check failed for staged {name}:\n{detail[-4000:]}")
    return problems


@contextlib.contextmanager
def _snapshot_worktree(root: Path, revision: str):
    """Check out a staged snapshot so file-scanning guards see the index, not unstaged edits."""
    git_env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_") or key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    with tempfile.TemporaryDirectory(prefix="aiflow-hook-") as temp:
        worktree = Path(temp) / "snapshot"
        added = subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(worktree), revision],
                               cwd=root, capture_output=True, text=True, env=git_env)
        if added.returncode:
            raise RuntimeError(f"cannot create staged snapshot worktree: {added.stderr.strip()[-300:]}")
        try:
            yield worktree
        finally:
            removed = subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root,
                                     capture_output=True, text=True, env=git_env)
            if removed.returncode:
                raise RuntimeError(f"cannot remove staged snapshot worktree: {removed.stderr.strip()[-300:]}")


def _duplicate_blocks(ctx: Context, snapshot: str) -> list[str]:
    from . import duplicate_blocks

    with _snapshot_worktree(ctx.path, snapshot) as root:
        return list(duplicate_blocks.check(root, f"origin/{ctx.base}").failures)


def _sql_boundary(ctx: Context, snapshot: str) -> list[str]:
    from . import sql_boundary

    with _snapshot_worktree(ctx.path, snapshot) as root:
        failures, _ = sql_boundary.check(root, f"origin/{ctx.base}")
        return failures


def _missing_gitleaks(ctx: Context) -> list[str]:
    return ["gitleaks is not installed: install it (free, https://github.com/gitleaks/gitleaks), then commit again"]


def _secret_step(ctx: Context) -> Step:
    if not (ctx.path / GITLEAKS_CONFIG).is_file():
        return Step("secrets", check=lambda _ctx: [f"required gitleaks allowlist is missing: {GITLEAKS_CONFIG}"])
    if shutil.which("gitleaks") is None:
        return Step("secrets", check=_missing_gitleaks)
    argv = ("gitleaks", "git", "--pre-commit", "--staged", "--redact", "--no-banner", "--exit-code", "1",
            "--log-level", "warn", "--config", GITLEAKS_CONFIG, ".")
    return Step("secrets", argv, "", 120)


def pre_commit_plan(ctx: Context) -> list[Step]:
    """The checks of a commit, cheapest first; the first failure stops the run.

    Skipped checks are left out and named in the output."""
    plan = _commit_checks(ctx)
    skipped = {name.strip() for name in os.environ.get(SKIP_VARIABLE, "").split(",") if name.strip()}
    if skipped:
        print(f"pre-commit: not run ({SKIP_VARIABLE}): " + ", ".join(s.name for s in plan if s.name in skipped),
              file=sys.stderr)
    return [s for s in plan if s.name not in skipped]


def _commit_checks(ctx: Context) -> list[Step]:
    head = staged_snapshot(ctx.path)
    ref = f"origin/{ctx.base}"
    plan = [Step("whitespace", ("git", "diff", "--cached", "--check"), "", 60)]
    modules = staged_python_modules(ctx.path)
    if modules:
        plan.append(Step("lint", check=_lint_staged))
    plan += [
        _secret_step(ctx),
        Step("giant-files", (PY, "-m", "aiflow.v4.core.giant_files", "--base", ref, "--head", head), "", 300),
        Step("duplicate-blocks", check=lambda context: _duplicate_blocks(context, head)),
        Step("broad-except", (PY, "-m", "aiflow.v4.core.broad_except", "check", "--base", ref, "--head", head), "",
             300),
        Step("sql-boundary", check=lambda context: _sql_boundary(context, head)),
    ]
    return plan


def affected_test_files(changed: list[str], existing: set[str]) -> tuple[list[str], list[str]]:
    """The v4 test files a push affects, and the changed modules that no test file is named after.

    A changed test file runs itself. A changed module ``aiflow/v4/<...>/<name>.py`` runs ``tests/v4/test_<name>.py``.
    The hook scripts run ``test_git_hooks.py``.
    """
    affected: list[str] = []
    unmapped: list[str] = []
    for path in changed:
        if path in existing:
            affected.append(path)
        elif path.startswith(".githooks/"):
            affected.append("tests/v4/test_git_hooks.py")
        elif path.startswith("aiflow/v4/") and path.endswith(".py"):
            named = f"tests/v4/test_{Path(path).stem}.py"
            if named in existing:
                affected.append(named)
            else:
                unmapped.append(path)
    return sorted(set(affected)), unmapped


def _existing_v4_tests(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in (root / TESTS).rglob("test_*.py")}


def _is_zero_sha(value: str) -> bool:
    return bool(value) and not value.strip("0")


def _changed_files_for_pushes(ctx: Context, push_refs: list[tuple[str, str, str, str]] | None) -> list[str]:
    if not push_refs:
        return _git(ctx.path, "diff", "--name-only", "--diff-filter=ACMR",
                    f"origin/{ctx.base}...HEAD").splitlines()
    changed: set[str] = set()
    for _local_ref, local_sha, _remote_ref, remote_sha in push_refs:
        if _is_zero_sha(local_sha):
            continue  # deleting a remote ref does not push a new commit
        if _is_zero_sha(remote_sha):
            base = _git(ctx.path, "merge-base", f"origin/{ctx.base}", local_sha)
        else:
            base = remote_sha
        files = _git(ctx.path, "diff", "--name-only", "--diff-filter=ACMR", base, local_sha).splitlines()
        changed.update(files)
    return sorted(changed)


def pre_push_plan(ctx: Context, push_refs: list[tuple[str, str, str, str]] | None = None) -> list[Step]:
    """The smoke tests, then the v4 tests changed by the pushed refs."""
    changed = _changed_files_for_pushes(ctx, push_refs)
    affected, unmapped = affected_test_files(changed, _existing_v4_tests(ctx.path))
    plan = [Step("smoke", (*UV_PYTEST, *SMOKE_TESTS, "-p", "no:warnings"), "", 600)]
    if affected:
        plan.append(Step("affected-tests", (*UV_PYTEST, *affected, "-p", "no:warnings"), "", 1200, gate.TEST_SERVICES))
    if unmapped:
        print("pre-push: no v4 test is named after " + ", ".join(unmapped) + "; CI runs them", file=sys.stderr)
    return plan


def short_report(result: GateResult) -> str:
    lines = [f"{s.status:<11} {s.name} ({s.seconds:.0f} s)" for s in result.steps]
    for stopped in [result.failed, *result.incomplete]:
        if stopped:
            kept = "\n".join(stopped.tail.splitlines()[-SCREEN_LINES:])
            lines += ["", f"--- {stopped.name}: {stopped.status} ---", kept]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="git_hooks", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="hook", required=True)
    for name in ("pre-commit", "pre-push"):
        hook = sub.add_parser(name, help=f"the {name} hook")
        hook.add_argument("--path", default=".", help="worktree to check")
        hook.add_argument("--base", default=os.environ.get("AIFLOW_BASE", "develop"), help="base branch")
        if name == "pre-push":
            hook.add_argument("--push-stdin", action="store_true", help="read Git's ref-update records from stdin")
    args = parser.parse_args(argv)
    ctx = Context(Path(args.path).resolve(), args.base)
    try:
        if args.hook == "pre-commit":
            plan = pre_commit_plan(ctx)
        else:
            refs = None
            if args.push_stdin:
                refs = []
                head = _git(ctx.path, "rev-parse", "HEAD")
                for line in sys.stdin:
                    fields = line.split()
                    if not fields:
                        continue
                    if len(fields) != 4:
                        raise RuntimeError("invalid pre-push ref record; expected local-ref local-sha remote-ref remote-sha")
                    local_ref, local_sha, remote_ref, remote_sha = fields
                    if not _is_zero_sha(local_sha) and local_sha != head:
                        raise RuntimeError(
                            f"the gate checks the checked-out commit ({head}) but you push {local_ref} ({local_sha}); "
                            "check that branch out first."
                        )
                    refs.append((local_ref, local_sha, remote_ref, remote_sha))
            plan = pre_push_plan(ctx, refs)
        result = gate.run(ctx, plan, log=lambda line: print(line, file=sys.stderr, flush=True))
    except (OSError, RuntimeError) as exc:
        print(f"{args.hook} could not run: {exc}", file=sys.stderr)
        return 1
    if result.exit_code:
        print(f"{args.hook} refused (JAIF-581). Fix the problem, then run it again.")
    print(short_report(result))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
