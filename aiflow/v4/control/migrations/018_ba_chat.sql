-- Conversation state and durable handoff for channel-independent BA brainstorming.
CREATE TABLE IF NOT EXISTS ba_chat_conversations (
    id              uuid PRIMARY KEY,
    channel_id      bigint NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
    external_chat_id text NOT NULL,
    project_key     text NOT NULL,
    status          text NOT NULL DEFAULT 'collecting'
                    CHECK (status IN ('collecting','proposal','creating','completed','rejected')),
    history         jsonb NOT NULL DEFAULT '[]'::jsonb,
    proposal        jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel_id, external_chat_id)
);

CREATE TABLE IF NOT EXISTS ba_chat_updates (
    channel_id      bigint NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
    update_id       bigint NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, update_id)
);

CREATE TABLE IF NOT EXISTS ba_chat_offsets (
    channel_id      bigint PRIMARY KEY REFERENCES notification_channels(id) ON DELETE CASCADE,
    next_update_id  bigint NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ba_chat_jobs (
    id              uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES ba_chat_conversations(id) ON DELETE CASCADE,
    channel_id      bigint NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
    update_id       bigint NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','failed')),
    claimed_by      text NOT NULL DEFAULT '',
    claimed_node    text NOT NULL DEFAULT '',
    claim_until     timestamptz,
    last_error      text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel_id, update_id)
);
CREATE INDEX IF NOT EXISTS ba_chat_jobs_due ON ba_chat_jobs (created_at, id)
    WHERE status IN ('pending','claimed');

CREATE TABLE IF NOT EXISTS ba_chat_outbox (
    id              bigserial PRIMARY KEY,
    channel_id      bigint NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
    conversation_id uuid REFERENCES ba_chat_conversations(id) ON DELETE CASCADE,
    body            text NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz
);
CREATE INDEX IF NOT EXISTS ba_chat_outbox_due ON ba_chat_outbox (next_attempt_at, id)
    WHERE status IN ('pending','sending');
