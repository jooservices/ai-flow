-- Security hardening (docs/v4/security.md): two-factor sign-in, sessions that can be ended, security settings, audit log.
ALTER TABLE console_users ADD COLUMN IF NOT EXISTS totp_secret text NOT NULL DEFAULT '';      -- AES-GCM ciphertext, never plain
ALTER TABLE console_users ADD COLUMN IF NOT EXISTS totp_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE console_users ADD COLUMN IF NOT EXISTS totp_last_step bigint NOT NULL DEFAULT 0; -- a code works once

-- Recovery codes are stored as keyed hashes (the key is outside the database) and each works once.
CREATE TABLE IF NOT EXISTS console_recovery_codes (
    email text NOT NULL REFERENCES console_users (email) ON DELETE CASCADE,
    code_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    used_at timestamptz,
    PRIMARY KEY (email, code_hash)
);

-- One row per signed-in browser; a signed cookie names its row, and a revoked or expired row ends the session.
CREATE TABLE IF NOT EXISTS console_sessions (
    sid text PRIMARY KEY,
    email text NOT NULL,
    role text NOT NULL,
    ip text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS console_sessions_email ON console_sessions (email, created_at DESC);

CREATE TABLE IF NOT EXISTS security_settings (
    key text PRIMARY KEY,
    value text NOT NULL,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The audit log is append-only: the console has no write path to it and the database refuses UPDATE, DELETE and TRUNCATE.
CREATE TABLE IF NOT EXISTS audit_log (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL DEFAULT '',
    category text NOT NULL,                 -- auth | people | roles | credentials | settings | security
    action text NOT NULL,
    target text NOT NULL DEFAULT '',
    ip text NOT NULL DEFAULT '',
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS audit_log_category ON audit_log (category, id DESC);

CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();
