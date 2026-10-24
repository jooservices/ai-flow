-- Persist the latest safe machine/project readiness snapshot and the time it was checked.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS readiness jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS readiness_checked_at timestamptz;
