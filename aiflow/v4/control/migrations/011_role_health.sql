-- Role health (docs/v4/role-health.md): a role switched off for one AI account, its probation tickets and the
-- thresholds the owner edits on screen.

CREATE TABLE IF NOT EXISTS role_health_settings (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    window_runs         integer NOT NULL DEFAULT 10,
    failure_percent     integer NOT NULL DEFAULT 60,
    updated_by          text NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS role_health (
    account_id          text NOT NULL,
    role                text NOT NULL,
    enabled             boolean NOT NULL DEFAULT true,
    note                text NOT NULL DEFAULT '',
    score               numeric(6, 4),                   -- failure score when the role was switched off
    disabled_at         timestamptz,
    scoring_from        timestamptz,                     -- runs started before this no longer count
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, role)
);

CREATE TABLE IF NOT EXISTS role_probation (
    id                  bigserial PRIMARY KEY,
    account_id          text NOT NULL,
    role                text NOT NULL,
    ticket_key          text NOT NULL,
    run_id              uuid,
    assigned_at         timestamptz NOT NULL DEFAULT now(),
    result              text NOT NULL DEFAULT '',        -- '' (open) | success | failure
    resolved_at         timestamptz
);
CREATE INDEX IF NOT EXISTS role_probation_role ON role_probation (account_id, role, assigned_at DESC);
