-- Merge queue: verified pull requests merge one after another per repository (docs/v4/merge-conflicts.md).
CREATE TABLE IF NOT EXISTS merge_queue (
    repo        text NOT NULL,
    number      integer NOT NULL,
    ticket_key  text NOT NULL,
    run_id      text NOT NULL DEFAULT '',
    state       text NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'merged', 'left')),
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    touched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, number)
);
CREATE INDEX IF NOT EXISTS merge_queue_waiting ON merge_queue (repo, enqueued_at) WHERE state = 'waiting';
