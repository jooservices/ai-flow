-- Durable coordinator handoffs and bounded node resource telemetry for run diagnostics.
CREATE TABLE IF NOT EXISTS node_heartbeat_samples (
    id              bigserial PRIMARY KEY,
    node            text NOT NULL,
    heartbeat_at    timestamptz NOT NULL DEFAULT now(),
    metrics         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS node_heartbeat_samples_latest
    ON node_heartbeat_samples (node, heartbeat_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS node_heartbeat_samples_retention
    ON node_heartbeat_samples (heartbeat_at);

CREATE TABLE IF NOT EXISTS ticket_handoffs (
    ticket_key      text PRIMARY KEY,
    incident_id     bigint NOT NULL UNIQUE REFERENCES incidents(id),
    state           text NOT NULL CHECK (state IN ('blocked', 'resolved')),
    handoff_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    resolved_by     text NOT NULL DEFAULT '',
    resolution      text NOT NULL DEFAULT ''
);
