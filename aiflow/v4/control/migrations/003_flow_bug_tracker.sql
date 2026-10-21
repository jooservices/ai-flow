-- Tracks which failed runs have already been reported for each open flow-bug signature.
-- `in_intake` stays false until the created Bug has reached Intake once, so a failed move is retried on later
-- master cycles without creating a second Bug for the same signature.
CREATE TABLE IF NOT EXISTS flow_bug_tracker (
    signature       text PRIMARY KEY,
    ticket_key      text NOT NULL,
    run_ids         text[] NOT NULL DEFAULT '{}',
    repeat_count    integer NOT NULL DEFAULT 0,
    in_intake       boolean NOT NULL DEFAULT false,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
