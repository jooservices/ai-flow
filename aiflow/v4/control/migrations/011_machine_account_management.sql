ALTER TABLE nodes ADD COLUMN IF NOT EXISTS accepting_work boolean NOT NULL DEFAULT true;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS removed_at timestamptz;

CREATE TABLE IF NOT EXISTS managed_ai_accounts (
    id                  text PRIMARY KEY,
    provider            text NOT NULL,
    node                text NOT NULL,
    tier                text NOT NULL DEFAULT 'middle',
    roles               text[] NOT NULL DEFAULT '{}',
    parallel            integer NOT NULL DEFAULT 1 CHECK (parallel BETWEEN 1 AND 8),
    explicit_only       boolean NOT NULL DEFAULT false,
    paused              boolean NOT NULL DEFAULT false,
    sign_in_url         text NOT NULL DEFAULT '',
    sign_in_code        text NOT NULL DEFAULT '',
    sign_in_expires_at  timestamptz,
    sign_in_started_at  timestamptz,
    removed_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS managed_ai_accounts_node ON managed_ai_accounts (node);
