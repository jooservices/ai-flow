-- Tracks which failed runs have already been reported for each open flow-bug signature.
CREATE TABLE IF NOT EXISTS flow_bug_tracker (
    signature       text PRIMARY KEY,
    ticket_key      text NOT NULL,
    run_ids         text[] NOT NULL DEFAULT '{}',
    repeat_count    integer NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
