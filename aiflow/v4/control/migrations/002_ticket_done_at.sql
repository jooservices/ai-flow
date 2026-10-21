-- Preserve the first observed completion time for ticket lead-time reports.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS done_at timestamptz;
UPDATE tickets SET done_at = jira_updated WHERE status = 'Done' AND done_at IS NULL;
