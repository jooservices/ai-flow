ALTER TABLE nodes ADD COLUMN IF NOT EXISTS projects text[] NOT NULL DEFAULT '{}';
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS dynamic boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS node_join_tokens (
    token_hash text PRIMARY KEY,
    node_name text NOT NULL,
    role text NOT NULL,
    project text NOT NULL,
    dynamic boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS node_join_tokens_pending_node ON node_join_tokens (node_name) WHERE used_at IS NULL;
