-- Memory: past work the AIs can search before they start (full text now, embeddings later).
-- Filled by control-loops from tickets, finished run summaries and verify findings; one row per (kind, ref).

CREATE TABLE IF NOT EXISTS memory (
    id              bigserial PRIMARY KEY,
    project         text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('ticket', 'run_summary', 'finding', 'answer')),
    ref             text NOT NULL,                       -- ticket key, or run id for run_summary and finding
    ticket          text NOT NULL DEFAULT '',            -- ticket the entry belongs to (a claim skips its own ticket)
    text            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_kind_ref ON memory (kind, ref);
CREATE INDEX IF NOT EXISTS memory_tsv ON memory USING gin (tsv);
CREATE INDEX IF NOT EXISTS memory_project ON memory (project, kind);
