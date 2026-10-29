-- Environment-configured owner escalation delivery; one database-wide hourly slot groups pending questions.
CREATE TABLE IF NOT EXISTS owner_escalation_notice_state (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    last_attempt_at timestamptz,
    last_sent_at timestamptz
);
INSERT INTO owner_escalation_notice_state (id) VALUES (true) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS owner_escalation_notices (
    id bigserial PRIMARY KEY,
    decision_id bigint NOT NULL,
    ticket_key text NOT NULL,
    question text NOT NULL,
    ticket_url text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    UNIQUE (decision_id, ticket_key)
);
CREATE INDEX IF NOT EXISTS owner_escalation_notices_pending
    ON owner_escalation_notices (id) WHERE sent_at IS NULL;
