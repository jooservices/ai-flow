-- Owner-requested install/repair actions are delivered to node agents and retained for the Command Center log.
CREATE TABLE IF NOT EXISTS node_operations (
    id              uuid PRIMARY KEY,
    node            text NOT NULL,
    action          text NOT NULL,                       -- install | repair
    actor           text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'queued',      -- queued | running | completed | failed
    result          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    completed_at    timestamptz
);
CREATE INDEX IF NOT EXISTS node_operations_node_created ON node_operations (node, created_at DESC);
CREATE INDEX IF NOT EXISTS node_operations_pending ON node_operations (node, created_at) WHERE status = 'queued';

-- Join capabilities are one-time and stored only as SHA-256 hashes; no SSH credential is accepted or retained.
CREATE TABLE IF NOT EXISTS node_join_tokens (
    token_hash      text PRIMARY KEY,
    node            text NOT NULL,
    expires_at      timestamptz NOT NULL,
    consumed_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS node_join_tokens_expiry ON node_join_tokens (expires_at);

COMMENT ON COLUMN write_audit.target IS 'jira | github | node';
