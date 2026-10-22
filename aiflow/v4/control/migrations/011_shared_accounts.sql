-- Shared AI accounts (docs/v4/shared-accounts.md): an account exists apart from machines and is enabled on several.
-- Only the sign-in prompt (a provider link and a short-lived device code) is kept here; credentials never leave
-- the machines that signed in.
CREATE TABLE IF NOT EXISTS shared_accounts (
    id              text PRIMARY KEY,
    provider        text NOT NULL,
    tier            text NOT NULL DEFAULT 'middle',
    roles           text[] NOT NULL DEFAULT '{}',
    explicit_only   boolean NOT NULL DEFAULT false,
    session_limit   integer NOT NULL DEFAULT 1 CHECK (session_limit BETWEEN 1 AND 50),
    managed         boolean NOT NULL DEFAULT true,   -- false: defined in aiflow-control; only the limit and machines are added here
    created_by      text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account_machines (
    account_id            text NOT NULL,
    node                  text NOT NULL,
    status                text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'signed_in')),
    signin_link           text NOT NULL DEFAULT '',
    signin_code           text NOT NULL DEFAULT '',
    signin_expires_at     timestamptz,
    signed_in_at          timestamptz,
    credential_expires_at timestamptz,
    worker_state          jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled_by            text NOT NULL DEFAULT '',
    enabled_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, node)
);
