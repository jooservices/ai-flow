"""Pull-request guard for duplicate blocks of normalized Python source lines.

    python3 -m aiflow.v4.core.duplicate_blocks --base origin/develop

The committed baseline may shrink as duplicate blocks are removed, but it may not gain findings or occurrences.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

SOURCE = "aiflow/v4"
BASELINE = "docs/v4/duplicate-blocks-baseline.json"
MIN_BLOCK_LINES = 8
_WINDOW_BASE = 1_000_003
_MASK = (1 << 64) - 1

LineRecord = tuple[str, int]
SourceLocation = tuple[str, int]
Block = tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    fingerprint: str
    occurrences: tuple[SourceLocation, ...]


@dataclass(frozen=True)
class CheckReport:
    findings: tuple[Finding, ...]
    failures: tuple[str, ...]
    shrinkage: tuple[str, ...]


def normalize_source(source: str) -> list[LineRecord]:
    """Keep nonblank, non-comment source lines with their original line numbers."""
    normalized = []
    for number, raw_line in enumerate(source.splitlines(), 1):
        line = raw_line.strip()
        if line and not line.startswith("#"):
            normalized.append((line, number))
    return normalized


def _rolling_hashes(lines: Sequence[str]) -> list[int]:
    """Return a deterministic rolling hash for every eight-line window."""
    if len(lines) < MIN_BLOCK_LINES:
        return []
    tokens = [int.from_bytes(hashlib.sha256(line.encode("utf-8")).digest()[:8], "big") for line in lines]
    power = pow(_WINDOW_BASE, MIN_BLOCK_LINES - 1, 1 << 64)
    current = 0
    for token in tokens[:MIN_BLOCK_LINES]:
        current = (current * _WINDOW_BASE + token) & _MASK
    hashes = [current]
    for index in range(MIN_BLOCK_LINES, len(tokens)):
        current = ((current - tokens[index - MIN_BLOCK_LINES] * power) * _WINDOW_BASE + tokens[index]) & _MASK
        hashes.append(current)
    return hashes


def _extend_match(left: Sequence[str], left_start: int, right: Sequence[str], right_start: int) -> tuple[int, int, int]:
    """Extend a matching seed window to the maximal equal block in both sources."""
    while left_start > 0 and right_start > 0 and left[left_start - 1] == right[right_start - 1]:
        left_start -= 1
        right_start -= 1
    length = MIN_BLOCK_LINES
    while left_start + length < len(left) and right_start + length < len(right):
        if left[left_start + length] != right[right_start + length]:
            break
        length += 1
    return left_start, right_start, length


def _candidate_occurrences(indexed: Mapping[str, list[LineRecord]]) -> dict[Block, set[SourceLocation]]:
    buckets: dict[int, list[tuple[str, int]]] = defaultdict(list)
    text_by_path = {path: [line for line, _ in records] for path, records in indexed.items()}
    for path in sorted(text_by_path):
        for start, value in enumerate(_rolling_hashes(text_by_path[path])):
            buckets[value].append((path, start))

    candidates: dict[Block, set[SourceLocation]] = defaultdict(set)
    for positions in buckets.values():
        for left_index, (left_path, left_start) in enumerate(positions):
            left_lines = text_by_path[left_path]
            for right_path, right_start in positions[left_index + 1:]:
                right_lines = text_by_path[right_path]
                if left_path == right_path and right_start - left_start < MIN_BLOCK_LINES:
                    continue
                left_window = left_lines[left_start:left_start + MIN_BLOCK_LINES]
                right_window = right_lines[right_start:right_start + MIN_BLOCK_LINES]
                if left_window != right_window:
                    continue
                first, second, length = _extend_match(left_lines, left_start, right_lines, right_start)
                if left_path == right_path and first + length > second:
                    continue
                block = tuple(left_lines[first:first + length])
                candidates[block].update(((left_path, first), (right_path, second)))
    return candidates


def _covers(larger: Block, large_location: SourceLocation, smaller: Block, small_location: SourceLocation) -> bool:
    if large_location[0] != small_location[0] or len(larger) <= len(smaller):
        return False
    offset = small_location[1] - large_location[1]
    return 0 <= offset <= len(larger) - len(smaller) and larger[offset:offset + len(smaller)] == smaller


def _maximal_candidates(candidates: Mapping[Block, set[SourceLocation]]) -> list[tuple[Block, set[SourceLocation]]]:
    selected: list[tuple[Block, set[SourceLocation]]] = []
    ordered = sorted(candidates.items(), key=lambda item: (-len(item[0]), item[0]))
    for block, occurrences in ordered:
        remaining = {
            occurrence for occurrence in occurrences
            if not any(_covers(larger, large_location, block, occurrence)
                       for larger, locations in selected for large_location in locations)
        }
        if len(remaining) >= 2:
            selected.append((block, remaining))
    return selected


def find_duplicates(sources: Mapping[str, str]) -> tuple[Finding, ...]:
    """Find maximal duplicate blocks in a mapping of source paths to Python text."""
    indexed = {path: normalize_source(source) for path, source in sources.items()}
    candidates = _candidate_occurrences(indexed)
    grouped: dict[str, set[SourceLocation]] = defaultdict(set)
    for block, positions in _maximal_candidates(candidates):
        text = "\n".join(block).encode("utf-8")
        fingerprint = hashlib.sha256(text).hexdigest()[:16]
        grouped[fingerprint].update((path, indexed[path][index][1]) for path, index in positions)
    return tuple(Finding(key, tuple(sorted(value))) for key, value in sorted(grouped.items()))


def scan_tree(root: Path) -> tuple[Finding, ...]:
    """Scan all Python files under aiflow/v4."""
    root = Path(root).resolve()
    source_root = root / SOURCE
    sources = {}
    for path in sorted(source_root.rglob("*.py")):
        sources[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return find_duplicates(sources)


def evaluate(findings: Sequence[Finding], baseline: Mapping[str, int]) -> tuple[list[str], list[str]]:
    """Return policy failures and baseline entries that can be removed or reduced."""
    current = {finding.fingerprint: len(finding.occurrences) for finding in findings}
    failures = [f"new fingerprint {key}: {count} occurrences not in baseline"
                for key, count in sorted(current.items()) if key not in baseline]
    failures.extend(f"{key}: occurrences rose from {baseline[key]} to {count}"
                    for key, count in sorted(current.items()) if key in baseline and count > baseline[key])
    shrinkage = [f"{key}: removed (was {count})" for key, count in sorted(baseline.items()) if key not in current]
    shrinkage.extend(f"{key}: occurrences fell from {count} to {current[key]}"
                     for key, count in sorted(baseline.items()) if key in current and current[key] < count)
    return failures, shrinkage


def baseline_growth(current: Mapping[str, int], previous: Mapping[str, int]) -> list[str]:
    """Reject baseline entries or occurrence counts added since the base revision."""
    problems = []
    for fingerprint, count in sorted(current.items()):
        old_count = previous.get(fingerprint)
        if old_count is None:
            problems.append(f"baseline adds fingerprint {fingerprint}")
        elif count > old_count:
            problems.append(f"baseline {fingerprint} rises from {old_count} to {count}")
    return problems


def _decode_baseline(text: str) -> dict[str, int]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("baseline must be a JSON object")
    for fingerprint, count in value.items():
        if (not isinstance(fingerprint, str) or len(fingerprint) != 16
                or any(char not in "0123456789abcdef" for char in fingerprint)
                or not isinstance(count, int) or isinstance(count, bool) or count < 2):
            raise ValueError("baseline entries must map 16-character fingerprints to occurrence counts of at least 2")
    return value


def _revision_baseline(root: Path, base: str) -> tuple[dict[str, int] | None, str | None]:
    verified = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=root,
                              capture_output=True, text=True)
    if verified.returncode:
        return None, f"base {base} does not resolve to a commit"
    shown = subprocess.run(["git", "show", f"{base}:{BASELINE}"], cwd=root, capture_output=True, text=True)
    if shown.returncode:
        return None, None
    try:
        return _decode_baseline(shown.stdout), None
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"baseline at {base} is invalid: {exc}"


def check(root: Path, base: str) -> CheckReport:
    """Compare source findings and the committed baseline with the requested base revision."""
    findings = scan_tree(root)
    baseline_path = Path(root) / BASELINE
    try:
        baseline = _decode_baseline(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"cannot read {BASELINE}: {exc}") from exc
    failures, shrinkage = evaluate(findings, baseline)
    previous, problem = _revision_baseline(Path(root), base)
    if problem:
        failures.append(problem)
    elif previous is not None:
        failures.extend(baseline_growth(baseline, previous))
    return CheckReport(findings, tuple(failures), tuple(shrinkage))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="base Git ref whose duplicate-block baseline may not grow")
    parser.add_argument("--root", default=".", help="repository root")
    args = parser.parse_args(argv)
    try:
        report = check(Path(args.root).resolve(), args.base)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Could not check duplicate blocks: {exc}", file=sys.stderr)
        return 2

    print("Duplicate blocks (fingerprint, occurrences and starting lines):")
    if report.findings:
        for finding in report.findings:
            print(f"- {finding.fingerprint}: {len(finding.occurrences)} occurrences")
            for path, line in finding.occurrences:
                print(f"  - {path}:{line}")
    else:
        print("- none")
    if report.shrinkage:
        print("Baseline entries that can shrink:")
        for item in report.shrinkage:
            print(f"- {item}")
    if report.failures:
        print("Duplicate-block check failed:")
        for item in report.failures:
            print(f"- {item}")
        return 1
    print("Duplicate-block check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
