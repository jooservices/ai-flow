"""The read brief is checked against the worktree: no file, function or path is cited unless it is in the code (JAIF-584)."""
from __future__ import annotations

import io
import sys

from aiflow.v4.core import read_brief

SOURCE = "def brief(run):\n    return run\n\nclass Holder:\n    def value(self):\n        return 1\n"


def _worktree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "docs.md").write_text("The Routes heading lists each route.\n", encoding="utf-8")
    return tmp_path


def test_read_lines_parse_the_path_and_each_named_function():
    text = "Notes\nREAD: pkg/mod.py: brief, Holder.value\nREAD: docs.md: Routes\n"
    assert read_brief.read_lines(text) == [("pkg/mod.py", ["brief", "Holder.value"]), ("docs.md", ["Routes"])]
    assert read_brief.read_lines("no brief here") == []


def test_a_brief_that_names_real_code_passes(tmp_path):
    root = _worktree(tmp_path)
    text = "READ: pkg/mod.py: brief, value\nREAD: docs.md: Routes\n"
    assert read_brief.check_brief(text, root) == []


def test_a_function_the_file_does_not_define_is_reported(tmp_path):
    root = _worktree(tmp_path)
    problems = read_brief.check_brief("READ: pkg/mod.py: brief, invented_helper\n", root)
    assert problems == ["pkg/mod.py: invented_helper is not defined there"]


def test_a_missing_file_and_a_path_outside_the_worktree_are_reported(tmp_path):
    root = _worktree(tmp_path)
    text = "READ: pkg/absent.py: brief\nREAD: ../outside.py: brief\n"
    assert read_brief.check_brief(text, root) == [
        "pkg/absent.py: file not found", "../outside.py: outside the worktree"]


def test_a_read_line_without_a_name_is_reported(tmp_path):
    root = _worktree(tmp_path)
    assert read_brief.check_brief("READ: pkg/mod.py\n", root) == ["pkg/mod.py: names no function or class to read"]


def test_a_brief_without_any_read_line_is_reported(tmp_path):
    assert read_brief.check_brief("I read everything.", tmp_path) == ["the brief has no READ line"]


def test_a_non_python_file_is_checked_for_the_name_in_its_text(tmp_path):
    root = _worktree(tmp_path)
    assert read_brief.check_brief("READ: docs.md: Routes, Missing\n", root) == [
        "docs.md: Missing does not appear in the file"]


def test_main_reads_stdin_and_exits_with_the_check_result(tmp_path, monkeypatch, capsys):
    _worktree(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("READ: pkg/mod.py: brief\n"))
    assert read_brief.main() == 0
    assert capsys.readouterr().out.strip() == "READ check passed"

    monkeypatch.setattr(sys, "stdin", io.StringIO("READ: pkg/mod.py: nope\n"))
    assert read_brief.main() == 1
    assert "READ check failed: 1 problem(s)" in capsys.readouterr().out
