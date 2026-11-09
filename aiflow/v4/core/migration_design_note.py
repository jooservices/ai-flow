"""Guard for the design note at the top of every control-plane migration (JAIF-600).

A migration file starts with SQL comment lines that answer five questions, each on a line that begins with its label:
``Purpose:``, ``Access patterns and indexes:``, ``Growth and retention:``, ``Compatibility:`` and ``Rollback:``.
Migrations written before the rule are listed in ``docs/v4/migration-design-note-baseline.json``. The list may only
shrink: a listed migration that gains a note must be removed from it, and no new name may be added.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

MIGRATIONS = "aiflow/v4/control/migrations"
BASELINE = "docs/v4/migration-design-note-baseline.json"
NOTE_LABELS = ("Purpose", "Access patterns and indexes", "Growth and retention", "Compatibility", "Rollback")


def _leading_comment_lines(sql: str) -> list[str]:
    lines: list[str] = []
    for raw in sql.splitlines():
        stripped = raw.strip()
        if stripped.startswith("--"):
            lines.append(stripped.removeprefix("--").strip())
        elif stripped:
            break
    return lines


def missing_note_labels(sql: str) -> list[str]:
    """Return the labels the leading comment block does not answer with text on the label line."""
    comment = _leading_comment_lines(sql)
    return [label for label in NOTE_LABELS
            if not any(re.match(rf"{re.escape(label)}:\s*\S", line, re.I) for line in comment)]


def migrations_without_note(directory: Path) -> list[str]:
    """Return the sorted names of migration files whose design note is missing or incomplete."""
    return [path.name for path in sorted(directory.glob("*.sql"))
            if missing_note_labels(path.read_text(encoding="utf-8"))]


def read_baseline(text: str) -> list[str]:
    """Parse the baseline file: a JSON list of migration file names."""
    value = json.loads(text)
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        raise ValueError(f"{BASELINE} must be a list of migration file names")
    return value


def evaluate(without_note: list[str], baseline: list[str]) -> list[str]:
    """Return the problems: migrations lacking a note and not listed, and listed names that no longer need it."""
    listed = set(baseline)
    problems = [f"{name}: no complete design note (needs {', '.join(NOTE_LABELS)})"
                for name in without_note if name not in listed]
    problems += [f"baseline lists {name}, which has a design note or no longer exists; remove it"
                 for name in sorted(listed - set(without_note))]
    return problems


def baseline_at_revision(root: Path, revision: str) -> list[str] | None:
    """Read the baseline at a Git revision, or return None before the baseline was introduced."""
    result = subprocess.run(["git", "show", f"{revision}:{BASELINE}"], cwd=root,
                            capture_output=True, text=True, check=False)
    return read_baseline(result.stdout) if result.returncode == 0 else None


def baseline_growth(baseline: list[str], previous_baseline: list[str] | None,
                    base_migrations: set[str]) -> list[str]:
    """Reject baseline additions, allowing only legacy migrations during the baseline's initial introduction."""
    if previous_baseline is not None:
        return [f"baseline adds {name}; the baseline may only shrink"
                for name in sorted(set(baseline) - set(previous_baseline))]
    return [f"baseline adds {name}; a new migration must carry its design note instead"
            for name in sorted(set(baseline) - base_migrations)]


def migrations_at_revision(root: Path, revision: str) -> set[str]:
    """List the migration file names in a Git revision (empty when the revision is unknown)."""
    result = subprocess.run(["git", "ls-tree", "--name-only", revision, f"{MIGRATIONS}/"], cwd=root,
                            capture_output=True, text=True, check=False)
    return {Path(line).name for line in result.stdout.splitlines()} if result.returncode == 0 else set()
