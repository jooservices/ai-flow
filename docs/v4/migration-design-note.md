# Migration design note

Every file in `aiflow/v4/control/migrations/` starts with SQL comment lines that answer five questions. Each answer is on
one line that begins with its label and has text after it:

- `-- Purpose:` what the change is for.
- `-- Access patterns and indexes:` the queries that read and write the data, and the keys and indexes that serve them.
- `-- Growth and retention:` the expected row growth and how old rows are removed.
- `-- Compatibility:` how the previous release keeps working (add, backfill, switch, drop later).
- `-- Rollback:` how the change is undone.

The note must come before the first statement. `tests/v4/test_migration_design_note.py` fails a pull request that adds
a migration without a complete note. The check lives in `aiflow/v4/core/migration_design_note.py`.

## Baseline

The migrations written before the rule are listed by file name in `docs/v4/migration-design-note-baseline.json`. They are
not edited. The list may only shrink: a test fails when it names a migration that is gone or already has a note, and
when it adds an entry compared with the pull request base. When first introducing the baseline, every entry must name a
migration present in that base. To add a rule step, extend `NOTE_LABELS` in the guard module and add the label to the
migrations that are not in the baseline.
