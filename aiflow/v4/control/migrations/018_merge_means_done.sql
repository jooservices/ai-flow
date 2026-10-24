-- Merged means Done (JAIF-267): the pull request state of each ticket, the merge facts and the recorded cause of
-- every ticket move.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS pr_url text NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS pr_state text NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS pr_checked_at timestamptz;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS approved_at timestamptz;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS merged_at timestamptz;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS merge_sha text NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS merged_by text NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS merge_note text NOT NULL DEFAULT '';

-- Who or what moved a ticket: 'flow merge', 'person', 'reconcile' or 'flow' (any other step of the flow).
CREATE TABLE IF NOT EXISTS ticket_moves (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    ticket      text NOT NULL,
    from_status text NOT NULL DEFAULT '',
    to_status   text NOT NULL,
    cause       text NOT NULL,
    detail      text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ticket_moves_ticket ON ticket_moves (ticket, id);

-- Tickets whose Jira state differs from their pull request on GitHub and the flow has not yet fixed.
CREATE TABLE IF NOT EXISTS ticket_disagreements (
    ticket       text PRIMARY KEY,
    since        timestamptz NOT NULL DEFAULT now(),
    jira_status  text NOT NULL DEFAULT '',
    github_state text NOT NULL DEFAULT '',
    expected     text NOT NULL DEFAULT '',
    detail       text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now()
);
