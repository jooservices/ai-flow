"""Check the read brief of a developer run against the code it names (JAIF-584).

The brief has one line per file the AI read before it wrote code::

    READ: aiflow/v4/worker/dev.py: brief, build_prompt

Every named file must exist inside the worktree and every named function must be defined in it, so the AI cannot
rely on code it did not find. The developer runs the check on its brief from the worktree::

    python3 -m aiflow.v4.core.read_brief <<'EOF'
    READ: aiflow/v4/worker/dev.py: brief, build_prompt
    EOF
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

READ_LINE = re.compile(r"^READ:[ \t]*([^:\n]+?)[ \t]*(?::[ \t]*([^\n]*))?$", re.M)


def read_lines(text: str) -> list[tuple[str, list[str]]]:
    """(path, function names) for each READ line; a line without names keeps an empty list."""
    return [(path, [name.strip() for name in names.split(",") if name.strip()])
            for path, names in READ_LINE.findall(text or "")]


def defined_names(source: str) -> set[str]:
    """Names of the functions and classes defined anywhere in a Python source."""
    tree = ast.parse(source)
    return {node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def missing_names(path: str, target: Path, names: list[str]) -> list[str]:
    """Names the file does not define; a non-Python file is checked for the name appearing in its text."""
    source = target.read_text(encoding="utf-8")
    if target.suffix != ".py":
        return [f"{path}: {name} does not appear in the file" for name in names if name not in source]
    known = defined_names(source)
    return [f"{path}: {name} is not defined there" for name in names if name not in known]


def check_read_line(root: Path, path: str, names: list[str]) -> list[str]:
    """Problems with one READ line: a missing file, a path outside the worktree or a name that is not in the file."""
    if not names:
        return [f"{path}: names no function or class to read"]
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        return [f"{path}: outside the worktree"]
    if not target.is_file():
        return [f"{path}: file not found"]
    return missing_names(path, target, names)


def check_brief(text: str, root: Path) -> list[str]:
    """Every problem with the brief; an empty list means each named file and function was found."""
    lines = read_lines(text)
    if not lines:
        return ["the brief has no READ line"]
    return [problem for path, names in lines for problem in check_read_line(root, path, names)]


def main() -> int:
    problems = check_brief(sys.stdin.read(), Path.cwd())
    for problem in problems:
        print(f"READ check: {problem}")
    print("READ check passed" if not problems else f"READ check failed: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
