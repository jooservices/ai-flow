"""JAIF-600: every migration carries a design note; migrations older than the rule are in a shrink-only baseline."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from aiflow.v4.core import migration_design_note as guard

ROOT = Path(__file__).resolve().parents[2]
ZERO_SHA = "0" * 40
COMPLETE_NOTE = """-- Purpose: remember why a ticket was held.
-- Access patterns and indexes: read by ticket key; a partial unique index keeps one active hold.
-- Growth and retention: one row per hold; rows older than a year may be pruned.
-- Compatibility: additive table, the previous release ignores it.
-- Rollback: drop the table; nothing else reads it.
CREATE TABLE IF NOT EXISTS retry_holds (id bigserial PRIMARY KEY);
"""


def _event_base_revision(event: dict[str, object]) -> str | None:
    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        base = pull_request.get("base")
        if isinstance(base, dict) and isinstance(base.get("sha"), str):
            return base["sha"]

    merge_group = event.get("merge_group")
    if isinstance(merge_group, dict) and isinstance(merge_group.get("base_sha"), str):
        return merge_group["base_sha"]

    before = event.get("before")
    if isinstance(before, str) and before and before != ZERO_SHA:
        return before
    return None


def _base_revision(root: Path) -> str:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        revision = _event_base_revision(event)
        if revision:
            present = subprocess.run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], cwd=root,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if present.returncode:
                fetched = subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin", revision],
                                         cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         check=False)
                assert fetched.returncode == 0, "GitHub event base commit could not be fetched"
            return revision

    present = subprocess.run(["git", "cat-file", "-e", "origin/develop^{commit}"], cwd=root,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if present.returncode:
        fetched = subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin",
                                  "develop:refs/remotes/origin/develop"], cwd=root,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        assert fetched.returncode == 0, "origin/develop could not be fetched"
    return "origin/develop"


def test_complete_note_has_no_missing_label():
    assert guard.missing_note_labels(COMPLETE_NOTE) == []


def test_file_without_comment_misses_every_label():
    assert guard.missing_note_labels("CREATE TABLE t (id int);\n") == list(guard.NOTE_LABELS)


def test_label_without_text_counts_as_missing():
    sql = COMPLETE_NOTE.replace("-- Rollback: drop the table; nothing else reads it.", "-- Rollback:")

    assert guard.missing_note_labels(sql) == ["Rollback"]


def test_comment_after_the_first_statement_is_not_part_of_the_header():
    sql = "CREATE TABLE t (id int);\n" + COMPLETE_NOTE

    assert guard.missing_note_labels(sql) == list(guard.NOTE_LABELS)


def test_migrations_without_note_lists_only_incomplete_files(tmp_path):
    (tmp_path / "001_good.sql").write_text(COMPLETE_NOTE, encoding="utf-8")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")

    assert guard.migrations_without_note(tmp_path) == ["002_bad.sql"]


def test_new_migration_without_note_fails():
    assert guard.evaluate(["039_new.sql"], []) == [
        "039_new.sql: no complete design note (needs Purpose, Access patterns and indexes, Growth and retention, "
        "Compatibility, Rollback)"]


def test_baseline_entry_that_gained_a_note_must_be_removed():
    problems = guard.evaluate([], ["001_old.sql"])

    assert len(problems) == 1 and "001_old.sql" in problems[0]


def test_listed_migration_without_note_passes():
    assert guard.evaluate(["001_old.sql"], ["001_old.sql"]) == []


def test_initial_baseline_rejects_entries_that_are_not_base_migrations():
    assert guard.baseline_growth(["001_old.sql", "039_new.sql"], None, {"001_old.sql"}) == [
        "baseline adds 039_new.sql; a new migration must carry its design note instead"]


def test_existing_baseline_can_only_shrink():
    assert guard.baseline_growth(["001_old.sql", "002_old.sql"], ["001_old.sql"],
                                 {"001_old.sql", "002_old.sql"}) == [
        "baseline adds 002_old.sql; the baseline may only shrink"]


def test_baseline_file_must_be_a_list_of_names():
    with pytest.raises(ValueError):
        guard.read_baseline('{"001_old.sql": 1}')


def test_event_base_revision_uses_pull_request_base_sha():
    assert _event_base_revision({"pull_request": {"base": {"sha": "base-sha"}}}) == "base-sha"


def test_event_base_revision_uses_merge_group_base_sha():
    assert _event_base_revision({"merge_group": {"base_sha": "merge-base-sha"}}) == "merge-base-sha"


def test_event_base_revision_uses_previous_push_revision():
    assert _event_base_revision({"before": "before-sha"}) == "before-sha"
    assert _event_base_revision({"before": ZERO_SHA}) is None


def test_every_migration_has_a_note_or_is_in_the_baseline():
    """AC1 and AC2: a pull request that adds a migration without a note fails here."""
    baseline = guard.read_baseline((ROOT / guard.BASELINE).read_text(encoding="utf-8"))
    without_note = guard.migrations_without_note(ROOT / guard.MIGRATIONS)

    assert guard.evaluate(without_note, baseline) == []


def test_baseline_only_shrinks_from_the_base_revision():
    base_revision = _base_revision(ROOT)
    base_migrations = guard.migrations_at_revision(ROOT, base_revision)
    assert base_migrations, f"{base_revision} migrations are unavailable; cannot verify the baseline"
    baseline = guard.read_baseline((ROOT / guard.BASELINE).read_text(encoding="utf-8"))
    previous_baseline = guard.baseline_at_revision(ROOT, base_revision)

    assert guard.baseline_growth(baseline, previous_baseline, base_migrations) == []


def test_migration_design_note_topic_page_is_linked_from_deploy_readme():
    readme = (ROOT / "deploy/v4/README.md").read_text(encoding="utf-8")
    topics = readme.split("## Topics", 1)[1].split("\n## ", 1)[0]

    assert (ROOT / "docs/v4/migration-design-note.md").is_file()
    assert "[Migration design note](../../docs/v4/migration-design-note.md)" in topics
