-- A ticket held after its third consecutive unsuccessful run (JAIF-367). The hold itself is the Jira label
-- `aiflow-hold`; this table remembers when a hold started and ended so the streak restarts after a release.
CREATE TABLE IF NOT EXISTS retry_holds (
    id           bigserial PRIMARY KEY,
    ticket_key   text NOT NULL,
    step         text NOT NULL DEFAULT '',
    fails        integer NOT NULL,
    run_ids      text[] NOT NULL DEFAULT '{}',
    held_at      timestamptz NOT NULL DEFAULT now(),
    released_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS retry_holds_active ON retry_holds (ticket_key) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS retry_holds_ticket ON retry_holds (ticket_key, released_at DESC);
