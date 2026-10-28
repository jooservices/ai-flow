-- The moment Jira confirmed the first-run sample ticket in Intake; the step stays ready from this record.
ALTER TABLE setup_wizard_state ADD COLUMN IF NOT EXISTS sample_confirmed_at timestamptz;
