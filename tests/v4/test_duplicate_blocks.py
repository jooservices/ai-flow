"""Duplicate-block detection, shrink-only baseline policy and guard wiring."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from aiflow.v4.core import duplicate_blocks as guard
from aiflow.v4.core.local_gate import steps

ROOT = Path(__file__).resolve().parents[2]


def block(count: int = 8, prefix: str = "shared") -> str:
    return "\n".join(f"{prefix}_{index:02d} = {index}" for index in range(count))


def test_normalize_source_drops_blank_and_comment_lines_but_keeps_line_numbers():
    assert guard.normalize_source("  # comment\n  value = 1  \n\n    # another\nnext = 2\n") == [
        ("value = 1", 2), ("next = 2", 5),
    ]


def test_rolling_hashes_cover_each_eight_line_window():
    assert len(guard._rolling_hashes(block(10).splitlines())) == 3
    assert guard._rolling_hashes(block(7).splitlines()) == []


def test_extend_match_grows_a_seed_to_its_maximal_equal_block():
    lines = block(12).splitlines()
    left = ["before", *lines, "after"]
    right = ["other", *lines, "end"]
    assert guard._extend_match(left, 1, right, 1) == (1, 1, 12)


def test_finds_a_duplicate_of_eight_lines_and_reports_starting_lines():
    shared = block()
    found = guard.find_duplicates({"aiflow/v4/a.py": f"before = 1\n{shared}", "aiflow/v4/b.py": shared})
    assert len(found) == 1
    assert found[0].occurrences == (("aiflow/v4/a.py", 2), ("aiflow/v4/b.py", 1))


def test_seven_identical_lines_in_formerly_excluded_directories_are_not_a_finding(tmp_path):
    shared = block(7)
    for name in ("first.py", "second.py"):
        path = tmp_path / "aiflow/v4/vendor" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(shared, encoding="utf-8")
    assert guard.scan_tree(tmp_path) == ()


def test_blank_comment_and_outer_whitespace_lines_do_not_break_or_count_in_fixture_directory(tmp_path):
    shared = block()
    first = "\n# lead\n" + "\n  # note\n".join(f"  {line}  " for line in shared.splitlines())
    second = "\n".join(f"\t{line}\t\n\n# ignored" for line in shared.splitlines())
    for name, source in (("first.py", first), ("second.py", second)):
        path = tmp_path / "aiflow/v4/fixtures" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    finding, = guard.scan_tree(tmp_path)
    assert len(finding.occurrences) == 2
    assert finding.occurrences == (("aiflow/v4/fixtures/first.py", 3), ("aiflow/v4/fixtures/second.py", 1))


def test_overlapping_windows_merge_to_one_maximal_block():
    shared = block(40)
    source = f"{shared}\n# separator\n\n{shared}"
    finding, = guard.find_duplicates({"one.py": source})
    expected = hashlib.sha256(shared.encode()).hexdigest()[:16]
    assert finding.fingerprint == expected
    assert finding.occurrences == (("one.py", 1), ("one.py", 43))


def test_finds_duplicate_blocks_twice_in_one_file():
    shared = block()
    finding, = guard.find_duplicates({"same.py": f"{shared}\nseparator = 1\n{shared}"})
    assert finding.occurrences == (("same.py", 1), ("same.py", 10))


def test_evaluate_rejects_new_fingerprints_and_increased_occurrences_and_reports_shrinks():
    current = [guard.Finding("a" * 16, (("a.py", 1), ("b.py", 1), ("c.py", 1))),
               guard.Finding("b" * 16, (("a.py", 2), ("b.py", 2)))]
    failures, shrinkage = guard.evaluate(current, {"a" * 16: 2, "c" * 16: 2, "d" * 16: 4})
    assert failures == [f"new fingerprint {'b' * 16}: 2 occurrences not in baseline",
                        f"{'a' * 16}: occurrences rose from 2 to 3"]
    assert shrinkage == [f"{'c' * 16}: removed (was 2)", f"{'d' * 16}: removed (was 4)"]


def test_evaluate_reports_a_lower_occurrence_count():
    current = [guard.Finding("a" * 16, (("a.py", 1), ("b.py", 1)))]
    assert guard.evaluate(current, {"a" * 16: 3}) == ([], [f"{'a' * 16}: occurrences fell from 3 to 2"])


def test_baseline_growth_rejects_added_entries_and_counts():
    assert guard.baseline_growth({"a" * 16: 3, "b" * 16: 2}, {"a" * 16: 2}) == [
        f"baseline {'a' * 16} rises from 2 to 3", f"baseline adds fingerprint {'b' * 16}",
    ]


def test_scan_tree_finds_new_duplicate_across_ordinary_and_formerly_excluded_paths(tmp_path, capsys):
    shared = block()
    baseline = tmp_path / guard.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", guard.BASELINE], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: Seed empty duplicate baseline"], cwd=tmp_path, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/develop", "HEAD"], cwd=tmp_path, check=True)

    paths = {
        "aiflow/v4/core/ordinary.py": f"lead = 1\n{shared}",
        "aiflow/v4/vendor/nested/vendored.py": f"\n# heading\n{shared}",
    }
    for relative, source in paths.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    finding, = guard.scan_tree(tmp_path)
    expected = hashlib.sha256(shared.encode()).hexdigest()[:16]
    assert finding.fingerprint == expected
    assert finding.occurrences == (("aiflow/v4/core/ordinary.py", 2), ("aiflow/v4/vendor/nested/vendored.py", 3))
    assert guard.main(["--base", "origin/develop", "--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert expected in output
    assert "aiflow/v4/core/ordinary.py:2" in output
    assert "aiflow/v4/vendor/nested/vendored.py:3" in output
    assert "new fingerprint" in output


def test_scan_tree_includes_every_formerly_excluded_directory_name_and_only_v4_python(tmp_path):
    shared = block()
    ignored_names = (
        "generated", "vendor", "vendored", "fixture", "fixtures", "test-fixture", "test-fixtures",
        "test_fixtures", "testfixtures",
    )
    expected_paths = []
    for name in ignored_names:
        directory = tmp_path / "aiflow/v4/nested" / name
        for relative in ("first.py", "deeper/second.py"):
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(shared, encoding="utf-8")
            expected_paths.append((path.relative_to(tmp_path).as_posix(), 1))

    non_python = tmp_path / "aiflow/v4/vendor/ignored.txt"
    non_python.parent.mkdir(parents=True, exist_ok=True)
    non_python.write_text(shared, encoding="utf-8")
    outside_v4 = tmp_path / "aiflow/v3/generated/ignored.py"
    outside_v4.parent.mkdir(parents=True, exist_ok=True)
    outside_v4.write_text(shared, encoding="utf-8")

    finding, = guard.scan_tree(tmp_path)
    assert finding.occurrences == tuple(sorted(expected_paths))


def test_scan_tree_returns_no_findings_for_missing_or_empty_source_tree(tmp_path):
    assert guard.scan_tree(tmp_path) == ()
    (tmp_path / "aiflow/v4").mkdir(parents=True)
    assert guard.scan_tree(tmp_path) == ()


def test_scan_tree_does_not_silently_skip_unreadable_python_source(tmp_path, monkeypatch):
    source = tmp_path / "aiflow/v4/nested/source.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == source:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(PermissionError, match="denied"):
        guard.scan_tree(tmp_path)


def test_scan_tree_does_not_silently_skip_undecodable_python_source(tmp_path):
    source = tmp_path / "aiflow/v4/nested/source.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        guard.scan_tree(tmp_path)


def test_scan_tree_included_paths_keep_baseline_occurrence_policy(tmp_path):
    shared = block()
    paths = [tmp_path / f"aiflow/v4/fixtures/nested/{name}.py" for name in ("first", "second")]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(shared, encoding="utf-8")
    finding, = guard.scan_tree(tmp_path)
    baseline = {finding.fingerprint: 2}
    assert guard.evaluate((finding,), baseline) == ([], [])

    third = tmp_path / "aiflow/v4/fixtures/third.py"
    third.write_text(shared, encoding="utf-8")
    finding, = guard.scan_tree(tmp_path)
    assert guard.evaluate((finding,), baseline) == (
        [f"{finding.fingerprint}: occurrences rose from 2 to 3"], [],
    )

    third.unlink()
    paths[1].unlink()
    assert guard.evaluate(guard.scan_tree(tmp_path), baseline) == (
        [], [f"{finding.fingerprint}: removed (was 2)"],
    )


def test_main_reports_locations_and_exits_on_policy_failures(monkeypatch, capsys):
    finding = guard.Finding("a" * 16, (("aiflow/v4/a.py", 4), ("aiflow/v4/b.py", 9)))
    monkeypatch.setattr(guard, "check", lambda _root, _base: guard.CheckReport((finding,), ("new fingerprint",), ()))
    assert guard.main(["--base", "origin/develop"]) == 1
    output = capsys.readouterr().out
    assert "aiflow/v4/a.py:4" in output and "aiflow/v4/b.py:9" in output
    assert "Duplicate-block check failed" in output


def test_duplicate_blocks_is_wired_into_local_gate_and_ci():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    gate_commands = {step.argv for step in steps("develop") if step.name == "duplicate-blocks"}
    job = workflow.split("  duplicate-blocks:\n", 1)[1].split("\n  quality:\n", 1)[0]
    command = next(line.strip().removeprefix("run: ") for line in job.splitlines()
                   if line.strip().startswith("run: python3 -m aiflow.v4.core.duplicate_blocks"))
    ci_command = tuple(command.replace('"$BASE"', "origin/develop").split())
    assert gate_commands == {("python3", "-m", "aiflow.v4.core.duplicate_blocks", "--base", "origin/develop")}
    assert ci_command in gate_commands
    assert "duplicate-blocks:" in workflow


def test_check_uses_the_local_baseline_and_allows_its_shrinkage(tmp_path):
    shared = block()
    for name in ("a.py", "b.py"):
        path = tmp_path / "aiflow/v4" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(shared, encoding="utf-8")
    finding = guard.find_duplicates({"a.py": shared, "b.py": shared})[0]
    baseline = tmp_path / guard.BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({finding.fingerprint: 3}), encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: Seed duplicate baseline"], cwd=tmp_path, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/develop", "HEAD"], cwd=tmp_path, check=True)
    report = guard.check(tmp_path, "origin/develop")
    assert report.failures == ()
    assert report.shrinkage == (f"{finding.fingerprint}: occurrences fell from 3 to 2",)
