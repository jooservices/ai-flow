#!/usr/bin/env python3
"""Print pytest paths for the coverage job when the affected-test map falls back to the full suite.

Keeps the Coverage job under seven minutes: smoke tests, any changed test module, and test modules
that share a file stem with a changed path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SMOKE = ("tests/v4/test_install_smoke.py", "tests/v4/test_route_registry.py")


def select_paths(changed: list[str], available: set[str]) -> list[str]:
    """Return sorted smoke, changed-test and stem-matched paths that exist in *available*."""
    selected = {name for name in SMOKE if name in available}
    for name in changed:
        if name.startswith("tests/v4/test_") and name.endswith(".py") and name in available:
            selected.add(name)
            continue
        candidate = f"tests/v4/test_{Path(name).stem}.py"
        if candidate in available:
            selected.add(candidate)
    return sorted(selected)


def main() -> int:
    base = os.environ.get("BASE", "").strip()
    if not base:
        print("BASE is required", file=sys.stderr)
        return 2
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=ACDMRT", base, "HEAD"],
        text=True,
    ).splitlines()
    available = {path.as_posix() for path in Path("tests/v4").rglob("test_*.py")}
    print(" ".join(select_paths(changed, available)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
