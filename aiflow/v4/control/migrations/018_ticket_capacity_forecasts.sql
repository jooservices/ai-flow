-- Candidate polls compare ticket estimates with the most recent watchdog quota forecast without rereading history.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS quota_forecast jsonb NOT NULL DEFAULT '{}'::jsonb;
