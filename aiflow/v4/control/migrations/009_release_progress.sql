-- Jira change history kept for the release progress screens: when a Fix version was attached to a ticket,
-- and when the ticket first moved to In Progress.
CREATE TABLE IF NOT EXISTS ticket_version_added (
    ticket_key      text NOT NULL REFERENCES tickets(key) ON DELETE CASCADE,
    version_id      text NOT NULL,
    added_at        timestamptz NOT NULL,
    PRIMARY KEY (ticket_key, version_id)
);
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS first_in_progress_at timestamptz;
