"""Code-quality checks that do not depend on any third-party reviewer (JAIF-415).

    python3 -m aiflow.v4.core.quality_checks check --base origin/develop
    python3 -m aiflow.v4.core.quality_checks baseline --write

Three kinds of finding are counted: ruff rules (bugs, simplifications, complexity above 12), functions longer than
80 lines and imports that break the layer rules of ``docs/v4/architecture.md``. The findings that exist today are
recorded in ``docs/v4/quality-baseline.json`` as totals per rule. A change fails when a total grows, and the baseline
itself may only shrink, so moving a legacy function into a new module is allowed while adding a new violation is not.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

BASELINE = "docs/v4/quality-baseline.json"
SOURCE = "aiflow/v4"
FUNCTION_LINES = 80
LONG_FUNCTION = "long-function"
LAYER = "layer"
# (package prefix, imports it must not use); docsite is a build-time tool that may inspect the control app.
LAYER_RULES = (
    ("aiflow/v4/control/", ("aiflow.v4.worker", "aiflow.v4.node")),
    ("aiflow/v4/core/", ("aiflow.v4.control", "aiflow.v4.worker", "aiflow.v4.node")),
    ("aiflow/v4/adapters/", ("aiflow.v4.control", "aiflow.v4.worker", "aiflow.v4.node")),
)
LAYER_EXEMPT = ("aiflow/v4/core/docsite.py",)


def ruff_findings(root: Path) -> list[tuple[str, str]]:
    out = subprocess.run([sys.executable, "-m", "ruff", "check", SOURCE, "--output-format", "json", "--no-cache"],
                         cwd=root, capture_output=True, text=True)
    if out.returncode not in (0, 1):
        raise RuntimeError(f"ruff failed: {out.stderr.strip()[-300:]}")
    return [(str(Path(f["filename"]).resolve().relative_to(root.resolve())), f["code"] or "ruff")
            for f in json.loads(out.stdout or "[]")]


def _import_targets(tree: ast.AST, path: str) -> list[set[str]]:
    """One set of dotted names per import statement (the module and, for ``from``, each name under it)."""
    package = path.removesuffix(".py").replace("/", ".").split(".")[:-1]
    statements: list[set[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            statements.append({a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            statements.append({module, *(f"{module}.{a.name}" for a in node.names)})
    return statements


def _breaks(names: set[str], forbidden: tuple[str, ...]) -> bool:
    return any(n == f or n.startswith(f + ".") for n in names for f in forbidden)


def structure_findings(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for file in sorted((root / SOURCE).rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        found += [(rel, LONG_FUNCTION) for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.end_lineno - node.lineno + 1 > FUNCTION_LINES]
        if rel in LAYER_EXEMPT:
            continue
        for prefix, forbidden in LAYER_RULES:
            if rel.startswith(prefix):
                found += [(rel, LAYER) for names in _import_targets(tree, rel) if _breaks(names, forbidden)]
    return found


def collect(root: Path) -> list[tuple[str, str]]:
    return ruff_findings(root) + structure_findings(root)


def totals(findings: list[tuple[str, str]]) -> dict[str, int]:
    return dict(sorted(Counter(rule for _, rule in findings).items()))


def compare(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Rules whose total grew beyond the baseline."""
    return [f"{rule}: {n} now, {baseline.get(rule, 0)} allowed" for rule, n in sorted(current.items())
            if n > baseline.get(rule, 0)]


def baseline_growth(new: dict[str, int], old: dict[str, int]) -> list[str]:
    return [f"baseline {rule} rises from {old.get(rule, 0)} to {n}" for rule, n in sorted(new.items())
            if n > old.get(rule, 0)]


def _read(root: Path, ref: str | None) -> dict[str, int] | None:
    try:
        if ref:
            out = subprocess.run(["git", "show", f"{ref}:{BASELINE}"], cwd=root, capture_output=True, text=True)
            return json.loads(out.stdout) if out.returncode == 0 else None
        return json.loads((root / BASELINE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check(root: Path, base: str | None) -> list[str]:
    current = totals(collect(root))
    committed = _read(root, None)
    if committed is None:
        return [f"{BASELINE} is missing: run `python3 -m aiflow.v4.core.quality_checks baseline --write`"]
    problems = compare(current, committed)
    previous = _read(root, base) if base else None
    if previous is not None:
        problems += baseline_growth(committed, previous)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quality_checks", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--base", default="", help="git ref whose baseline the new baseline may not exceed")
    chk.add_argument("--root", default=".")
    base = sub.add_parser("baseline")
    base.add_argument("--write", action="store_true")
    base.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.command == "check":
        problems = check(root, args.base or None)
        if problems:
            print("Code quality check failed:\n- " + "\n- ".join(problems) +
                  "\nFix the new findings (`python3 -m ruff check aiflow/v4`), or move code so the totals do not grow.")
            return 1
        print("Code quality check passed.")
        return 0
    current = totals(collect(root))
    old = _read(root, None)
    if old is not None and (grown := baseline_growth(current, old)):
        print("Refusing to raise the baseline:\n- " + "\n- ".join(grown))
        return 1
    if args.write:
        (root / BASELINE).write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(current, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
