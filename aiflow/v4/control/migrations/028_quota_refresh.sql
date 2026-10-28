-- Account quota snapshots have explicit provenance and observation time. Run usage stays in runs;
-- a run without a provider quota observation must not manufacture a fresh sample from old state.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS quota_observed_at timestamptz;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS quota_source text NOT NULL DEFAULT '';

-- Historical rows do not say whether their quota came from a fresh provider read or a copied prior snapshot.
-- Keep them out of provider-sample rate learning until new, sourced observations arrive.
ALTER TABLE account_quota_samples ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'legacy';
ALTER TABLE account_quota_samples ADD COLUMN IF NOT EXISTS observed_at timestamptz;
UPDATE accounts SET quota_at_risk=false,quota_forecast='{}'::jsonb WHERE quota_source='';
UPDATE incidents i SET closed_at=now() FROM accounts a
 WHERE i.kind='quota_high' AND i.closed_at IS NULL AND a.id=split_part(i.subject,':',1) AND a.quota_source='';
-- Older lease-loss paths did not always record an end time. Anchor those unresolved estimates at upgrade time so
-- only a later provider snapshot can reconcile them.
UPDATE runs SET ended_at=now() WHERE status='lost' AND ended_at IS NULL;
CREATE INDEX IF NOT EXISTS runs_account_started ON runs (account_id,started_at DESC);
CREATE INDEX IF NOT EXISTS runs_account_ended_finished ON runs (account_id,ended_at DESC) WHERE status='finished';
CREATE INDEX IF NOT EXISTS runs_account_running ON runs (account_id) WHERE status='running';

CREATE TABLE IF NOT EXISTS quota_refresh_settings (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    interval_minutes    integer NOT NULL DEFAULT 15 CHECK (interval_minutes BETWEEN 1 AND 1440),
    updated_by          text NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO quota_refresh_settings (id, interval_minutes) VALUES (true, 15) ON CONFLICT (id) DO NOTHING;

-- One lease per logical account, including accounts enabled on multiple machines.
CREATE TABLE IF NOT EXISTS quota_refresh_jobs (
    account_id          text PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    next_due_at         timestamptz NOT NULL DEFAULT now(),
    lease_token         uuid,
    lease_owner         text NOT NULL DEFAULT '',
    lease_expires_at    timestamptz,
    last_attempt_at     timestamptz,
    last_success_at     timestamptz,
    last_error          text NOT NULL DEFAULT '',
    last_source         text NOT NULL DEFAULT '',
    last_observed_at    timestamptz
);
CREATE INDEX IF NOT EXISTS quota_refresh_due ON quota_refresh_jobs (next_due_at, lease_expires_at);
