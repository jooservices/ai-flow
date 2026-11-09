-- Bound per-machine worker test suites without coupling the setting to load-limit JSON updates.
-- Design note: purpose - one small per-node limit of project test suites that run at once. Access - read with the
-- node row by the heartbeat and the machines list, written by the load-limits route; no new index (nodes is a
-- handful of rows). Growth - one smallint per node, no retention needed. Compatibility - additive with a default of 2
-- and a CHECK of 1..32, so the previous release ignores it. Rollback - forward-only; the column can stay unused.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS test_suite_slots smallint NOT NULL DEFAULT 2
    CHECK (test_suite_slots BETWEEN 1 AND 32);
