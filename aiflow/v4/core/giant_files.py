"""Pull-request guard for oversized source files.

The develop tree is the baseline. Existing source files above ``LINE_LIMIT`` may only shrink, while a newly added
text file may not exceed the limit. The command is used by the pull-request workflow and has no runtime dependency
beyond Git and Python's standard library:

    python3 -m aiflow.v4.core.giant_files --base origin/develop --head HEAD
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import PurePosixPath

LINE_LIMIT = 1_500

# These are source formats in use or supported by the repository. Markdown and data fixtures are deliberately not
# considered baseline source files, although any newly added text file is still subject to the size limit.
SOURCE_SUFFIXES = frozenset({
    ".bash", ".c", ".cc", ".cpp", ".css", ".cjs", ".go", ".h", ".hpp", ".htm", ".html", ".java",
    ".js", ".jsx", ".kt", ".kts", ".less", ".mjs", ".php", ".py", ".pyi", ".rb", ".rs", ".sass",
    ".scss", ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml",
})


def _tree_blobs(revision: str) -> list[tuple[str, str]]:
    """Return regular-file (path, object-id) pairs from a Git tree."""
    raw = subprocess.check_output(["git", "ls-tree", "-r", "-z", "--full-tree", revision])
    entries: list[tuple[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        if kind == "blob" and mode in {"100644", "100755"}:
            entries.append((os.fsdecode(path_bytes), object_id))
    return entries


def _text_line_counts(entries: list[tuple[str, str]]) -> dict[str, int]:
    """Count lines in decodable text blobs in one batched Git read; binary blobs are ignored."""
    if not entries:
        return {}
    request = b"".join(object_id.encode("ascii") + b"\n" for _, object_id in entries)
    output = subprocess.check_output(["git", "cat-file", "--batch"], input=request)
    counts: dict[str, int] = {}
    cursor = 0
    for path, expected_id in entries:
        header_end = output.find(b"\n", cursor)
        if header_end < 0:
            raise RuntimeError("Git returned an incomplete blob header")
        object_id, kind, size_text = output[cursor:header_end].split()
        size = int(size_text)
        cursor = header_end + 1
        content = output[cursor:cursor + size]
        cursor += size + 1  # blob bytes and the protocol's trailing newline
        if object_id.decode("ascii") != expected_id or kind != b"blob":
            raise RuntimeError("Git returned a different object than requested")
        if b"\0" in content:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        counts[path] = len(text.splitlines())
    return counts


def line_counts(revision: str) -> tuple[dict[str, int], set[str]]:
    """Return text line counts and all regular paths in a Git tree."""
    entries = _tree_blobs(revision)
    return _text_line_counts(entries), {path for path, _ in entries}


def evaluate(
    baseline_source_counts: dict[str, int],
    baseline_paths: set[str],
    head_text_counts: dict[str, int],
) -> tuple[dict[str, int], list[tuple[str, str, int, int]]]:
    """Return over-limit baseline sources and failures (kind, path, baseline, pull-request line count)."""
    baseline_giants = {
        path: count for path, count in baseline_source_counts.items() if count > LINE_LIMIT
    }
    failures: list[tuple[str, str, int, int]] = []
    for path, baseline_count in sorted(baseline_giants.items()):
        head_count = head_text_counts.get(path)
        if head_count is not None and head_count > baseline_count:
            failures.append(("grown", path, baseline_count, head_count))
    for path, head_count in sorted(head_text_counts.items()):
        if path not in baseline_paths and head_count > LINE_LIMIT:
            failures.append(("new", path, 0, head_count))
    return baseline_giants, failures


def check(base: str, head: str) -> tuple[dict[str, int], list[tuple[str, str, int, int]]]:
    """Read two Git trees and apply the pull-request file-size policy."""
    baseline_text_counts, baseline_paths = line_counts(base)
    head_text_counts, _ = line_counts(head)
    baseline_source_counts = {
        path: count for path, count in baseline_text_counts.items()
        if PurePosixPath(path).suffix.lower() in SOURCE_SUFFIXES
    }
    return evaluate(baseline_source_counts, baseline_paths, head_text_counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="develop baseline Git ref or commit")
    parser.add_argument("--head", default="HEAD", help="pull-request Git ref or commit (default: HEAD)")
    args = parser.parse_args(argv)
    try:
        baseline_giants, failures = check(args.base, args.head)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Could not check giant files: {exc}", file=sys.stderr)
        return 2

    print(f"Develop baseline source files over {LINE_LIMIT:,} lines:")
    if baseline_giants:
        for path, count in sorted(baseline_giants.items()):
            print(f"- {path}: {count:,} lines")
    else:
        print("- none")
    if not failures:
        print("Giant-file check passed.")
        return 0

    print("Giant-file check failed:")
    for kind, path, baseline_count, head_count in failures:
        if kind == "grown":
            print(f"- {path} grew from {baseline_count:,} to {head_count:,} lines")
        else:
            print(f"- new file {path} has {head_count:,} lines (limit: {LINE_LIMIT:,})")
    print("Developer AI: put new code for the feature in a new module instead of extending an over-limit file.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
