-- Keep the project chosen when the setup sample issue was created so retries cannot relabel it.
ALTER TABLE setup_wizard_state ADD COLUMN IF NOT EXISTS sample_ticket_project text NOT NULL DEFAULT '';
