-- When a ticket was first held back for a hot file; the maximum wait is measured from here (docs/v4/merge-conflicts.md).
CREATE TABLE IF NOT EXISTS hot_file_blocked (
    ticket_key text PRIMARY KEY,
    blocked_at timestamptz NOT NULL DEFAULT now()
);
