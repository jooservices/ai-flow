"""Pull-request guard for SQL strings outside v4 data-access modules.

The check scans Python string literals with the AST, compares their per-file counts with a baseline, and allows the
baseline to shrink. The only accepted paths are aggregate repositories and the database boundary modules described
in ``docs/v4/architecture.md`` and ``docs/v4/event-architecture.md``.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping

BASELINE = "docs/v4/sql-outside-data-access-baseline.json"
SOURCE = "aiflow/v4"
ALLOWED_PATH_PATTERNS = (
    "aiflow/v4/control/features/*/repository.py",
    "aiflow/v4/control/repositories/*.py",
    "aiflow/v4/control/repositories/**/*.py",
    "aiflow/v4/control/db.py",
    "aiflow/v4/control/platform/db.py",
    "aiflow/v4/control/platform/session.py",
    "aiflow/v4/control/platform/data/*.py",
    "aiflow/v4/control/platform/data/**/*.py",
)
SQL_START = re.compile(
    r"^\s*(?:SELECT\b|INSERT\s+INTO\b|UPDATE\b[\s\S]*?\bSET\b|DELETE\s+FROM\b|"
    r"WITH\b[\s\S]*?\bAS(?:\s+(?:NOT\s+)?MATERIALIZED)?\s*\(|"
    r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?(?:TABLE|INDEX|TYPE|SCHEMA|VIEW|FUNCTION|TRIGGER|SEQUENCE|EXTENSION)\b|"
    r"ALTER\s+(?:TABLE|INDEX|TYPE|SCHEMA|VIEW|SEQUENCE)\b|"
    r"DROP\s+(?:TABLE|INDEX|TYPE|SCHEMA|VIEW|FUNCTION|TRIGGER|SEQUENCE|EXTENSION)\b|"
    r"TRUNCATE\s+(?:TABLE\s+)?[\w\"]+|COPY\s+[^\n]+\s+(?:FROM|TO)\b|"
    r"MERGE\s+INTO\b|REPLACE\s+INTO\b|EXPLAIN\b|GRANT\b|REVOKE\b|COMMENT\s+ON\b|CALL\b|DO\s+\$\$)",
    re.IGNORECASE,
)
SELECT_FROM = re.compile(
    r"\bSELECT\b[\s\S]*?\bFROM\s+(?:[\w$]+\s*\.\s*)?(?:[\w$]+|\"[^\"]+\")",
    re.IGNORECASE,
)


def is_data_access(path: str) -> bool:
    """Whether a repository-relative path is an architectural SQL owner."""
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in ALLOWED_PATH_PATTERNS)


def _docstring_ids(tree: ast.AST) -> set[int]:
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, scopes) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.add(id(first.value))
    return found


def is_sql_string(value: str) -> bool:
    """Recognize common SQL statements without matching explanatory prose."""
    return bool(SQL_START.search(value) or SELECT_FROM.search(value))


def count_sql_strings(source: str) -> int:
    """Count SQL string constants, including f-string parts, while ignoring Python docstrings."""
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings and is_sql_string(node.value)
    )


def scan_sources(sources: Mapping[str, str]) -> dict[str, int]:
    """Return SQL counts for files outside the data-access paths, omitting files with no SQL."""
    counts = {}
    for path, source in sorted(sources.items()):
        if not is_data_access(path) and (count := count_sql_strings(source)):
            counts[path] = count
    return counts


def scan_tree(root: Path) -> dict[str, int]:
    """Read Python files under the v4 source tree and return out-of-boundary SQL counts."""
    sources = {
        file.relative_to(root).as_posix(): file.read_text(encoding="utf-8")
        for file in sorted((root / SOURCE).rglob("*.py"))
    }
    return scan_sources(sources)


def evaluate(
    current: dict[str, int], baseline: dict[str, int], previous_baseline: dict[str, int] | None,
) -> tuple[list[str], list[str]]:
    """Return failures and shrink reports for current findings and baseline changes."""
    failures = []
    lowered = []
    for path, count in sorted(current.items()):
        allowed = baseline.get(path)
        if allowed is None:
            failures.append(f"{path}: {count} SQL string(s) have no baseline entry")
        elif count > allowed:
            failures.append(f"{path}: SQL count rose from {allowed} to {count}")
    for path, old_count in sorted(baseline.items()):
        count = current.get(path, 0)
        if count < old_count:
            lowered.append(f"{path}: {old_count} to {count}")
    if previous_baseline is not None:
        for path, count in sorted(baseline.items()):
            old_count = previous_baseline.get(path)
            if old_count is None:
                failures.append(f"baseline adds {path}: {count} SQL string(s)")
            elif count > old_count:
                failures.append(f"baseline count for {path} rises from {old_count} to {count}")
    return failures, lowered


def _read_baseline(path: Path) -> dict[str, int]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not isinstance(count, int) or isinstance(count, bool) or count < 1
        for name, count in value.items()
    ):
        raise ValueError(f"{path} must be a JSON object of positive per-file counts")
    return value


def _baseline_at(root: Path, revision: str) -> dict[str, int] | None:
    result = subprocess.run(
        ["git", "show", f"{revision}:{BASELINE}"], cwd=root, capture_output=True, text=True,
    )
    if result.returncode:
        return None
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not isinstance(count, int) or isinstance(count, bool) or count < 1
        for name, count in value.items()
    ):
        raise ValueError(f"{revision}:{BASELINE} must be a JSON object of positive per-file counts")
    return value


def check(root: Path, base: str) -> tuple[list[str], list[str]]:
    """Compare the worktree's SQL strings and baseline with the selected Git base."""
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        cwd=root, capture_output=True, text=True,
    )
    if resolved.returncode:
        raise ValueError(f"--base {base} does not resolve to a commit")
    baseline = _read_baseline(root / BASELINE)
    previous = _baseline_at(root, base)
    return evaluate(scan_tree(root), baseline, previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="base Git ref whose baseline may not be exceeded")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        failures, lowered = check(root, args.base)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
        print(f"Could not check SQL boundary: {exc}", file=sys.stderr)
        return 2
    for item in lowered:
        print(f"SQL baseline can shrink: {item}")
    if failures:
        print("SQL boundary check failed:")
        for item in failures:
            print(f"- {item}")
        return 1
    print("SQL boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
