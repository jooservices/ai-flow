-- A Dashboard request for one master review (JAIF-349). NULL means no request is waiting. The next snapshot that
-- reads it clears it, so the column holds at most one pending request and never grows.
ALTER TABLE master_settings ADD COLUMN IF NOT EXISTS review_requested_at timestamptz;

-- The Dashboard's master cycle metrics read master runs by start time; this partial index covers only those rows.
CREATE INDEX IF NOT EXISTS runs_master_started ON runs (started_at) WHERE step = 'master';
