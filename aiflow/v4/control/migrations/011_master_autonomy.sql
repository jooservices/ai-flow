-- Owner-configured master cycle limits, invalid-plan thresholds, temporary account pauses and idempotent alerts.
CREATE TABLE IF NOT EXISTS master_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    daily_budget_minutes integer NOT NULL DEFAULT 60 CHECK (daily_budget_minutes BETWEEN 0 AND 1440),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT ''
);
INSERT INTO master_settings (singleton, daily_budget_minutes) VALUES (true, 60)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS master_plan_cycles (
    run_id uuid PRIMARY KEY,
    valid boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS master_plan_cycles_time ON master_plan_cycles (recorded_at DESC);

CREATE TABLE IF NOT EXISTS master_account_pauses (
    account_id text PRIMARY KEY,
    paused_until timestamptz NOT NULL,
    reason text NOT NULL DEFAULT '',
    run_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS master_escalations (
    observation_key text PRIMARY KEY,
    ticket text NOT NULL DEFAULT '',
    reason text NOT NULL,
    options jsonb NOT NULL DEFAULT '[]'::jsonb,
    run_id uuid NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS master_escalations_time ON master_escalations (sent_at DESC);
