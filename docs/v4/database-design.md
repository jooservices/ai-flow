# Database design rules

The control plane keeps its state in one PostgreSQL database. Its schema is changed only by the numbered SQL files in
`aiflow/v4/control/migrations/`, applied by `DB.migrate` in `aiflow/v4/control/db.py`. Every new table, column, index
and constraint follows the rules below, and every pull request that adds a migration answers the checklist at the end
in its design note. The schema must be right before the first production release: a design that would have to change
after production is a defect of the highest severity.

## Naming

- Tables are plural `snake_case` nouns: `domain_events`, `node_heartbeat_samples`, `ticket_handoffs`.
- Columns are `snake_case`. Timestamps end in `_at` (`heartbeat_at`, `resolved_at`); a key to another table is named
  after that table's row (`incident_id`, `ticket_key`).
- Indexes are `<table>_<purpose>`: `node_heartbeat_samples_latest`, `domain_events_aggregate`,
  `domain_event_deliveries_due`. The name says which read it serves, not which columns it has.
- Migration files are `NNN_<topic>.sql`, with the next free three-digit number. The runner applies files in name order
  and the upgrade check refuses any other file name (`docs/v4/fleet-upgrade.md`). A file that no release has shipped
  may still be renamed to resolve a number clash; once a release has applied it, its content and name never change.

## Keys and constraints

- Every table has a primary key. Append-only, high-volume tables use `bigserial` (`node_heartbeat_samples.id`,
  `domain_events.seq`); a natural key that the product already names uses `text` (`ticket_handoffs.ticket_key`).
- State columns are `text NOT NULL` with a `CHECK (state IN (...))` list. Do not add a PostgreSQL `ENUM` type:
  none exists in the registry, and widening or narrowing a type is harder to migrate than a constraint.
- Every column is `NOT NULL` unless absence has a meaning the code reads. Text fields that may be empty default to
  `''`, numeric counters to `0`, and time columns to `now()`, so that an insert names only what it knows.
- A relationship is a foreign key. `ON DELETE CASCADE` is used only when the child row has no meaning without its
  parent (`domain_event_deliveries.event_id`). "One row per X" is a `UNIQUE` constraint on X, not a rule the code
  must remember (`ticket_handoffs.incident_id`).
- Rules that span rows, such as "only one open incident per ticket", are enforced by a unique or partial index, and the
  code relies on the database error rather than a read-then-write check.

## Indexes per access pattern

Before a table is created, write down each query that reads or changes it, with its filter, order and expected
frequency. For each access pattern, state whether an index is needed; name the index that serves it, or why none is
needed:

- A lookup by a key is served by the key's primary or unique index.
- A "latest row for an entity" read is an index on `(entity, time DESC, id DESC)`.
- A queue of work that is still pending is a partial index on the due time with `WHERE status = 'pending'`.
- A retention delete reads by time, so the table needs an index on its time column.

Each index costs writes on every insert, so an index that no listed query needs is removed from the design. The
design note names the query each new index serves.

## Growth and retention

Every table that can grow by more than a few thousand rows a month has a written growth estimate and a retention plan
before it ships. The estimate is rows per unit (per node, per ticket, per day) times the number of units. The plan is
one of:

- **Pruned**: a named job deletes rows older than a window, and the window is stated. Example:
  `node_heartbeat_samples` keeps 30 days through `prune_heartbeat_samples`, called on heartbeat writes; at one sample
  every 15 seconds it grows by about 2,880 rows per node per day before pruning. `flow_bug_signature_members` keeps
  90 days.
- **Kept**: the rows are evidence and are never deleted by the product. The reason is written in the table's design
  note.
- **Partitioned**: a time-partitioned table is planned only when pruning by `DELETE` becomes measurably slow. No table
  is partitioned today.

A high-volume table without a plan is not merged. Reads that must stay fast as data grows follow the rules in
`docs/v4/scale-proof-budgets.md`.

## Migrations are additive and forward only

A release never edits or deletes an applied migration (`docs/v4/fleet-upgrade.md`). There are no down migrations: a
rollback does not undo the schema, so the previous release must keep working on the migrated schema. A change is
therefore made in phases, each in its own release:

1. **Expand**: add the new table, column (nullable or with a default), index or constraint. Old code ignores it.
2. **Backfill**: fill the new column from the old data, in batches small enough not to hold locks for long. A migration
   file runs inside one transaction, so `CREATE INDEX CONCURRENTLY` cannot be used there; put a large index on a
   table that is still small, or build it in a batch job.
3. **Switch**: the new code reads and writes the new shape. The previous release still works, because the old column
   stays in place and is still written, or is nullable.
4. **Contract**: drop the old column, table or constraint in a later release, after no supported release reads it. The
   version skew policy in `docs/v4/fleet-upgrade.md` says which releases are supported.

Renaming a column is an add of the new name, a dual write, a switch and a drop, never a single `ALTER ... RENAME`.

## No superuser in application migrations

Migrations run with the role the control plane logs in with; that role is the product identity recorded in
`audit_product_roles`. An application migration does not require superuser rights. It must not use `ALTER SYSTEM`,
change roles, or create objects that need superuser (an event trigger, or an extension that is not trusted). A
migration that needs one of these is a decision for the owner, not a change that is merged silently. Two existing
migrations already need more than that: `005_memory_vectors.sql` runs `CREATE EXTENSION vector`, and
`018_flow_doctor.sql` creates two event triggers. Both are idempotent, and neither is a pattern to copy.

## Enumerations and JSON

- An enumeration is a `text` column with a `CHECK ... IN (...)` constraint. Adding a value is an expand step: replace
  the constraint with a wider list. Removing a value is a contract step, after no row holds it.
- A JSON column is `jsonb NOT NULL DEFAULT '{}'::jsonb`, written by the repository through the `J()` wrapper. It holds
  data that is read as a whole or shown to a person, such as `domain_events.payload` or the heartbeat metrics.
- A key that a query filters, joins or sorts on is a column, or an expression index on it. A JSON key is never the
  only place a rule is enforced.
- The owning feature's topic page documents the keys of each JSON document. Changing a documented key is an expand and
  contract change like a column.

## Checklist for a migration design note

Answer each line in the pull request description. A line with no answer blocks the review.

1. Which reads and writes will use the data? List each query, its filter and its order.
2. Which keys and constraints state the rules? Name each one, and say what happens when a rule is broken.
3. Which index serves each query? Name the index, and say which query it serves.
4. What are the expected rows per unit, and how many units? Is the table high-volume?
5. How are old rows removed (pruned, kept or partitioned), and with which window and job?
6. Which phase of expand, backfill, switch and contract does this release contain? Does the previous release still
   work on the migrated schema?
7. Does the migration run with the normal control role? If it needs more, name the statement and the owner's decision.
8. Are enumerations `text` with a `CHECK`, and JSON columns `jsonb` with a documented shape?
9. Does the file name follow `NNN_<topic>.sql`, and does it avoid edits to any applied file?
