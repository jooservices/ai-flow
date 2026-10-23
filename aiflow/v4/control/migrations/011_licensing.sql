CREATE TABLE IF NOT EXISTS licensing_state (
    id              smallint PRIMARY KEY CHECK (id = 1),
    installation_id text NOT NULL,
    plan            text NOT NULL DEFAULT 'free',
    expires_at      timestamptz,
    license_token   text NOT NULL DEFAULT '',
    license_source  text NOT NULL DEFAULT '',
    invalid_since   timestamptz,
    plan_limits     jsonb NOT NULL DEFAULT '{"free":{"machines":1,"ai_accounts":2,"projects":3,"users":3},"team":{"machines":5,"ai_accounts":12,"projects":25,"users":25},"business":{"machines":50,"ai_accounts":100,"projects":250,"users":250}}'::jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
