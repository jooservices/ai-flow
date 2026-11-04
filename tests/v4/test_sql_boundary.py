"""SQL boundary guard: AST scanning, data-access paths and shrink-only baseline policy."""
from __future__ import annotations

import subprocess
from pathlib import Path

from aiflow.v4.core import sql_boundary
from aiflow.v4.core.sql_boundary import BASELINE, count_sql_strings, evaluate, scan_sources, scan_tree


def _commit_files(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test baseline"],
        cwd=root,
        check=True,
    )


def test_sql_in_a_route_source_tree_fails_without_a_baseline_entry(tmp_path):
    route = tmp_path / "aiflow/v4/control/routes/report.py"
    route.parent.mkdir(parents=True)
    route.write_text('QUERY = "SELECT * FROM reports"\n', encoding="utf-8")
    current = scan_tree(tmp_path)
    failures, _ = evaluate(current, {}, None)
    assert current == {"aiflow/v4/control/routes/report.py": 1}
    assert "no baseline entry" in failures[0]


def test_repository_modules_are_data_access_paths(tmp_path):
    repository = tmp_path / "aiflow/v4/control/features/reports/repository.py"
    repository.parent.mkdir(parents=True)
    repository.write_text('QUERY = "SELECT * FROM reports"\n', encoding="utf-8")
    assert scan_tree(tmp_path) == {}
    assert scan_sources({"aiflow/v4/control/repositories/legacy.py": 'QUERY = "DELETE FROM reports"'}) == {}


def test_nested_feature_repository_is_not_a_data_access_path(tmp_path):
    repository = tmp_path / "aiflow/v4/control/features/reports/nested/repository.py"
    repository.parent.mkdir(parents=True)
    repository.write_text('QUERY = "SELECT * FROM reports"\n', encoding="utf-8")
    assert scan_tree(tmp_path) == {"aiflow/v4/control/features/reports/nested/repository.py": 1}


def test_module_class_and_function_docstrings_are_not_sql():
    source = '''
"""SELECT * FROM prose."""
class Example:
    """DELETE FROM imaginary."""
    def query(self):
        """INSERT INTO imaginary VALUES (1)."""
        return "ordinary text"
'''
    assert count_sql_strings(source) == 0


def test_ast_scanner_counts_implicit_concatenation_and_fstring_parts():
    source = 'plain = "SELECT " "* FROM reports"\nquery = f"SELECT * FROM {table}"\n'
    assert count_sql_strings(source) == 2


def test_ast_scanner_recognizes_common_sql_forms_and_select_from_fragments():
    source = '''
read = "SELECT 1"
insert = "INSERT INTO reports (id) VALUES (1)"
update = "UPDATE reports SET active = true"
delete = "DELETE FROM reports"
cte = "WITH picked AS (SELECT id FROM reports) SELECT id FROM picked"
fragment = "prefix SELECT id FROM reports"
create = "CREATE TABLE reports (id integer)"
copy = "COPY reports TO STDOUT"
explain = "EXPLAIN SELECT id FROM reports"
prose = "The report from this month is ready"
'''
    assert count_sql_strings(source) == 9


def test_ast_scanner_recognizes_create_unique_index():
    assert count_sql_strings('query = "CREATE UNIQUE INDEX reports_id_idx ON reports (id)"') == 1


def test_a_raised_sql_count_fails():
    failures, lowered = evaluate({"aiflow/v4/control/routes/report.py": 2},
                                 {"aiflow/v4/control/routes/report.py": 1}, None)
    assert failures == ["aiflow/v4/control/routes/report.py: SQL count rose from 1 to 2"]
    assert lowered == []


def test_a_lowered_sql_count_is_reported_without_failing():
    failures, lowered = evaluate({}, {"aiflow/v4/control/routes/report.py": 2}, None)
    assert failures == []
    assert lowered == ["aiflow/v4/control/routes/report.py: 2 to 0"]


def test_a_new_baseline_entry_fails_against_the_previous_baseline():
    path = "aiflow/v4/control/routes/report.py"
    failures, _ = evaluate({path: 1}, {path: 1}, {})
    assert failures == [f"baseline adds {path}: 1 SQL string(s)"]


def test_baseline_counts_may_shrink_but_not_rise():
    path = "aiflow/v4/control/routes/report.py"
    failures, _ = evaluate({path: 1}, {path: 1}, {path: 2})
    assert failures == []


def test_missing_previous_baseline_is_absent(tmp_path):
    _commit_files(tmp_path, {"tracked.txt": "present\n"})
    assert sql_boundary._baseline_at(tmp_path, "HEAD") is None


def test_failed_previous_baseline_read_returns_an_error(tmp_path, monkeypatch, capsys):
    _commit_files(tmp_path, {BASELINE: "{}\n"})
    original_run = subprocess.run

    def fail_show(command, *args, **kwargs):
        if command[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(command, 128, "", "unable to read object")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(sql_boundary.subprocess, "run", fail_show)
    assert sql_boundary.main(["--base", "HEAD", "--root", str(tmp_path)]) == 2
    assert "Could not read baseline at HEAD (git show exited 128)" in capsys.readouterr().err


def test_sql_boundary_is_wired_into_the_local_gate_and_ci():
    from aiflow.v4.core.local_gate import ci_commands, steps

    step = next(step for step in steps("develop") if step.name == "sql-boundary")
    assert step.argv == ("python3", "-m", "aiflow.v4.core.sql_boundary", "--base", "origin/develop")
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert ci_commands(workflow, ("sql-boundary",)) == [
        "python3 -m aiflow.v4.core.sql_boundary --base $BASE"
    ]
