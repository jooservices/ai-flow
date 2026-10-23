-- Historical provider quota snapshots support learned resets and account-specific burn forecasts.
CREATE TABLE IF NOT EXISTS account_quota_samples (
    id              bigserial PRIMARY KEY,
    account_id      text NOT NULL,
    provider        text NOT NULL DEFAULT '',
    model           text NOT NULL DEFAULT '',
    sampled_at      timestamptz NOT NULL DEFAULT now(),
    quota           jsonb NOT NULL DEFAULT '{}'::jsonb,
    tokens_in       bigint NOT NULL DEFAULT 0,
    tokens_out      bigint NOT NULL DEFAULT 0,
    cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
    duration_ms     bigint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS account_quota_samples_history ON account_quota_samples (account_id, sampled_at DESC);
