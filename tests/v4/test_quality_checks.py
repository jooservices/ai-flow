"""Tool-independent code-quality checks (JAIF-415)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from aiflow.v4.core import quality_checks as qc

ROOT = Path(__file__).resolve().parents[2]


def project(tmp_path, files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def long_function(lines=90):
    return "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(lines)) + "    return 1\n"


def test_long_function_and_layer_violations_are_counted(tmp_path):
    root = project(tmp_path, {
        "aiflow/v4/core/a.py": long_function(),
        "aiflow/v4/core/b.py": "from ..control import api\nfrom aiflow.v4.worker import dev\nimport os\n",
        "aiflow/v4/control/c.py": "from ..worker import dev\n",
        "aiflow/v4/control/ok.py": "from ..core import settings\n",
        "aiflow/v4/core/docsite.py": "from ..control import api\n",
        "aiflow/v4/adapters/d.py": "from ..core import x\n",
    })
    found = qc.structure_findings(root)
    assert qc.totals(found) == {"layer": 3, "long-function": 1}
    assert ("aiflow/v4/core/a.py", "long-function") in found


def test_a_function_at_the_limit_is_fine(tmp_path):
    root = project(tmp_path, {"aiflow/v4/core/a.py": long_function(qc.FUNCTION_LINES - 2)})
    assert qc.structure_findings(root) == []


def test_totals_must_not_grow_but_may_fall():
    assert qc.compare({"C901": 91}, {"C901": 91}) == []
    assert qc.compare({"C901": 90}, {"C901": 91}) == []
    assert qc.compare({"C901": 92, "layer": 1}, {"C901": 91}) == ["C901: 92 now, 91 allowed", "layer: 1 now, 0 allowed"]


def test_the_baseline_itself_may_only_shrink():
    assert qc.baseline_growth({"C901": 80}, {"C901": 91}) == []
    assert qc.baseline_growth({"C901": 92, "F401": 1}, {"C901": 91}) == [
        "baseline C901 rises from 91 to 92", "baseline F401 rises from 0 to 1"]


def run_cli(root, *args):
    return subprocess.run([sys.executable, "-m", "aiflow.v4.core.quality_checks", *args, "--root", str(root)],
                          cwd=ROOT, capture_output=True, text=True)


def test_cli_end_to_end_on_a_small_project(tmp_path):
    root = project(tmp_path, {"aiflow/v4/core/a.py": "import os\n", "docs/v4/placeholder.md": "x\n"})
    first = run_cli(root, "baseline", "--write")
    assert first.returncode == 0 and json.loads((root / qc.BASELINE).read_text()) == {"F401": 1}
    assert run_cli(root, "check").returncode == 0
    (root / "aiflow/v4/core/b.py").write_text("import sys\n")  # a new unused import: the total grows
    failed = run_cli(root, "check")
    assert failed.returncode == 1 and "F401: 2 now, 1 allowed" in failed.stdout
    refused = run_cli(root, "baseline", "--write")
    assert refused.returncode == 1 and "Refusing to raise the baseline" in refused.stdout


def test_a_missing_baseline_is_reported(tmp_path):
    root = project(tmp_path, {"aiflow/v4/core/a.py": "x = 1\n"})
    failed = run_cli(root, "check")
    assert failed.returncode == 1 and "baseline.json is missing" in failed.stdout


def test_the_repository_passes_its_own_check():
    result = run_cli(ROOT, "check")
    assert result.returncode == 0, result.stdout


def test_an_unresolvable_base_ref_fails_instead_of_skipping_the_growth_check(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "docs" / "v4").mkdir(parents=True)
    (tmp_path / qc.BASELINE).write_text("{}", encoding="utf-8")
    problems = qc.check(tmp_path, "origin/does-not-exist")
    assert any("does not resolve" in p for p in problems)
