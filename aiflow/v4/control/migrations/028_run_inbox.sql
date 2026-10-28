-- Owner messages queued for a live worker and applied after the current provider invocation finishes.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS result_received_at timestamptz;

CREATE TABLE IF NOT EXISTS run_inbox_messages (
    id              bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    client_id       uuid NOT NULL,
    ticket_key      text NOT NULL,
    project         text NOT NULL,
    author          text NOT NULL DEFAULT 'Console',
    kind            text NOT NULL DEFAULT 'context',
    body            text NOT NULL,
    status          text NOT NULL DEFAULT 'queued', -- queued | received | included | undelivered
    created_at      timestamptz NOT NULL DEFAULT now(),
    received_at     timestamptz,
    included_at     timestamptz,
    terminal_reason text NOT NULL DEFAULT '',
    CONSTRAINT run_inbox_kind CHECK (kind IN ('context', 'request', 'idea')),
    CONSTRAINT run_inbox_status CHECK (status IN ('queued', 'received', 'included', 'undelivered')),
    CONSTRAINT run_inbox_body_length CHECK (char_length(body) BETWEEN 1 AND 8000),
    CONSTRAINT run_inbox_client_once UNIQUE (run_id, client_id)
);
CREATE INDEX IF NOT EXISTS run_inbox_pending ON run_inbox_messages (run_id, id)
    WHERE status IN ('queued', 'received');
