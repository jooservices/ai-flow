"""Pull-request guard for unexplained broad exception handlers.

    python3 -m aiflow.v4.core.broad_except check --base origin/develop --head HEAD
    python3 -m aiflow.v4.core.broad_except baseline --from origin/develop --write

The checked-in per-file baseline may only shrink. Handlers added above its count fail, and handlers removed from a
file are reported so its baseline entry can be lowered.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

BASELINE = "docs/v4/broad-except-baseline.json"
SOURCE = "aiflow/v4/"
BROAD_NAMES = frozenset({"Exception", "BaseException"})
DIRECTIVE = re.compile(r"^(?:noqa(?::\s*[A-Z0-9_,]+)?|pylint:\s*disable=[A-Z0-9_,\-]+)", re.I)


def _broad_type(node: ast.expr | None) -> bool:
    if isinstance(node, ast.Name):
        return node.id in BROAD_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in BROAD_NAMES
    return isinstance(node, ast.Tuple) and any(_broad_type(item) for item in node.elts)


def _reason_comment(comment: str) -> bool:
    body = comment.removeprefix("#").strip()
    directive = DIRECTIVE.match(body)
    if directive:
        body = body[directive.end():].strip()
    return any(len(reason.strip()) >= 12 for marker in ("-", ":")
               for reason in body.split(marker)[1:])


def _comment_lines(source: str) -> dict[int, list[str]]:
    comments: dict[int, list[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.setdefault(token.start[0], []).append(token.string)
    return comments


def _handler_explained(handler: ast.ExceptHandler, comments: dict[int, list[str]]) -> bool:
    lines = {handler.lineno - 1, handler.lineno, handler.body[0].lineno}
    return any(_reason_comment(comment) for line in lines for comment in comments.get(line, []))


def unexplained_count(source: str) -> int:
    """Count broad exception handlers without a nearby explanatory comment."""
    tree = ast.parse(source)
    comments = _comment_lines(source)
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.ExceptHandler) and (node.type is None or _broad_type(node.type))
               and not _handler_explained(node, comments))


def counts_by_file(sources: dict[str, str]) -> dict[str, int]:
    """Return positive unexplained-handler counts for Python files, keyed by repository path."""
    return {path: count for path, source in sorted(sources.items())
            if (count := unexplained_count(source)) > 0}


def evaluate(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[tuple[str, int, int]]]:
    """Return violations and reductions (path, baseline count, current count)."""
    problems = [f"{path}: {count} unexplained broad handler(s), baseline allows {baseline.get(path, 0)}"
                for path, count in sorted(current.items()) if count > baseline.get(path, 0)]
    reduced = [(path, count, current.get(path, 0)) for path, count in sorted(baseline.items())
               if current.get(path, 0) < count]
    return problems, reduced


def baseline_growth(current: dict[str, int], previous: dict[str, int]) -> list[str]:
    """Return baseline entries that were added or increased from the base revision."""
    return [f"baseline {path} rises from {previous.get(path, 0)} to {count}"
            for path, count in sorted(current.items()) if count > previous.get(path, 0)]


def _tree_texts(root: Path, revision: str) -> dict[str, str]:
    raw = subprocess.check_output(["git", "ls-tree", "-r", "-z", "--full-tree", revision, "--", SOURCE,
                                   BASELINE], cwd=root)
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        path = path_bytes.decode("utf-8")
        if kind == "blob" and mode in {"100644", "100755"} and (path.endswith(".py") or path == BASELINE):
            entries.append((path, object_id))
    if not entries:
        return {}
    request = b"".join(object_id.encode("ascii") + b"\n" for _, object_id in entries)
    output = subprocess.check_output(["git", "cat-file", "--batch"], cwd=root, input=request)
    return _decode_blobs(output, entries)


def _decode_blobs(output: bytes, entries: list[tuple[str, str]]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    cursor = 0
    for path, expected_id in entries:
        header_end = output.find(b"\n", cursor)
        if header_end < 0:
            raise RuntimeError("Git returned an incomplete blob header")
        object_id, kind, size_text = output[cursor:header_end].split()
        size = int(size_text)
        cursor = header_end + 1
        content = output[cursor:cursor + size]
        cursor += size + 1
        if object_id.decode("ascii") != expected_id or kind != b"blob":
            raise RuntimeError("Git returned a different object than requested")
        decoded[path] = content.decode("utf-8")
    return decoded


def _source_files(tree: dict[str, str]) -> dict[str, str]:
    return {path: source for path, source in tree.items() if path.startswith(SOURCE) and path.endswith(".py")}


def _read_baseline(source: str | None) -> dict[str, int]:
    if source is None:
        raise ValueError(f"{BASELINE} is missing from the checked revision")
    value = json.loads(source)
    if not isinstance(value, dict) or any(not isinstance(path, str) or type(count) is not int or count < 1
                                          for path, count in value.items()):
        raise ValueError(f"{BASELINE} must map file paths to positive integer counts")
    return value


def check(
    root: Path, base: str, head: str
) -> tuple[dict[str, int], dict[str, int], list[tuple[str, int, int]], list[str]]:
    """Compare the checked-in baseline and handlers in two Git revisions."""
    base_tree, head_tree = _tree_texts(root, base), _tree_texts(root, head)
    current_baseline = _read_baseline(head_tree.get(BASELINE))
    current = counts_by_file(_source_files(head_tree))
    problems, reduced = evaluate(current, current_baseline)
    previous_text = base_tree.get(BASELINE)
    if previous_text is None:
        base_counts = counts_by_file(_source_files(base_tree))
        problems.extend(baseline_growth(current_baseline, base_counts))
    else:
        problems.extend(baseline_growth(current_baseline, _read_baseline(previous_text)))
    return current, current_baseline, reduced, problems


def _working_sources(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((root / SOURCE).rglob("*.py"))}


def _baseline_sources(root: Path, revision: str | None) -> dict[str, str]:
    return _source_files(_tree_texts(root, revision)) if revision else _working_sources(root)


def _render_baseline(counts: dict[str, int]) -> str:
    return json.dumps(dict(sorted(counts.items())), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--base", required=True)
    check_parser.add_argument("--head", default="HEAD")
    check_parser.add_argument("--root", default=".")
    baseline_parser = sub.add_parser("baseline")
    baseline_parser.add_argument("--from", dest="revision", default="")
    baseline_parser.add_argument("--write", action="store_true")
    baseline_parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "baseline":
            content = _render_baseline(counts_by_file(_baseline_sources(root, args.revision or None)))
            if args.write:
                (root / BASELINE).write_text(content, encoding="utf-8")
            print(content, end="")
            return 0
        current, baseline, reduced, problems = check(root, args.base, args.head)
    except (OSError, RuntimeError, ValueError, SyntaxError, tokenize.TokenError, subprocess.CalledProcessError) as exc:
        print(f"Could not check broad exceptions: {exc}", file=sys.stderr)
        return 2
    for path, allowed, found in reduced:
        print(f"Baseline can shrink: {path} from {allowed} to {found} unexplained handler(s)")
    if problems:
        print("Broad-except check failed:\n- " + "\n- ".join(problems))
        return 1
    print(f"Broad-except check passed ({len(current)} file(s) with findings, {len(baseline)} baseline file(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
