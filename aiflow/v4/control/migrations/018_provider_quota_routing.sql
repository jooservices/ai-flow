-- Provider quota routing (docs/v4/provider-routing.md): the owner's provider order and ceilings, the hold-back state
-- of each provider and the recorded reason for each assignment.

CREATE TABLE IF NOT EXISTS provider_routing_settings (
    id              boolean PRIMARY KEY DEFAULT true CHECK (id),
    provider_order  text[] NOT NULL DEFAULT '{}',
    ceilings        jsonb NOT NULL DEFAULT '{}'::jsonb,       -- provider -> percent of the quota window
    updated_by      text NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS provider_routing_state (
    provider        text PRIMARY KEY,
    held_since      timestamptz NOT NULL DEFAULT now(),
    window_name     text NOT NULL DEFAULT '',
    window_reset_at timestamptz,                               -- the held window's own reset time
    used_pct        numeric(6, 2)
);

CREATE TABLE IF NOT EXISTS routing_decisions (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    ticket_key      text NOT NULL,
    step            text NOT NULL,
    account_id      text NOT NULL,
    provider        text NOT NULL,
    kind            text NOT NULL,                             -- headroom | label | needed | fallback | only
    reason          text NOT NULL
);
CREATE INDEX IF NOT EXISTS routing_decisions_at_idx ON routing_decisions (at DESC);
CREATE INDEX IF NOT EXISTS routing_decisions_ticket_idx ON routing_decisions (ticket_key, at DESC);
