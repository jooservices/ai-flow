-- Join grants from older releases are short-lived, so invalidate them while upgrading their schema.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS projects text[] NOT NULL DEFAULT '{}';
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS dynamic boolean NOT NULL DEFAULT false;

DROP TABLE IF EXISTS node_join_tokens;
CREATE TABLE node_join_tokens (
    token_hash text PRIMARY KEY,
    node_name text NOT NULL,
    role text NOT NULL,
    project text NOT NULL,
    dynamic boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);
CREATE UNIQUE INDEX node_join_tokens_pending_node ON node_join_tokens (node_name) WHERE used_at IS NULL;
