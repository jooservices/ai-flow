-- Retry user-facing explanations after the underlying safe repair has already completed.
CREATE TABLE IF NOT EXISTS flow_doctor_comment_outbox (
    id              bigserial PRIMARY KEY,
    action_key      text NOT NULL UNIQUE,
    ticket_key      text NOT NULL,
    body            text NOT NULL,
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz
);
CREATE INDEX IF NOT EXISTS flow_doctor_comment_outbox_pending
    ON flow_doctor_comment_outbox (id) WHERE delivered_at IS NULL;
