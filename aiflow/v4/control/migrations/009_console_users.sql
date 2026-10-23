-- Console login (docs/v4/console-login.md): one row per person; the password is only stored as a salted scrypt hash.
CREATE TABLE IF NOT EXISTS console_users (
    email text PRIMARY KEY,
    display_name text NOT NULL DEFAULT '',
    role text NOT NULL,
    password_hash text NOT NULL,
    disabled boolean NOT NULL DEFAULT false,
    failed_attempts integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    last_login_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT console_users_email_lower CHECK (email = lower(email))
);
