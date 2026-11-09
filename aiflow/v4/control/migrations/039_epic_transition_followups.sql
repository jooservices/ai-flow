-- Durable delivery state for side effects produced by automatic epic transitions.
CREATE TABLE IF NOT EXISTS epic_transition_followups (
    id                    bigserial PRIMARY KEY,
    ticket                text NOT NULL,
    project               text NOT NULL,
    from_status           text NOT NULL,
    to_status             text NOT NULL,
    source                text NOT NULL,
    reason                text NOT NULL,
    child_count           integer NOT NULL,
    completed_at          timestamptz NOT NULL,
    comment               text NOT NULL DEFAULT '',
    ready                 boolean NOT NULL DEFAULT false,
    moved                 boolean NOT NULL DEFAULT true,
    status_event_done     boolean NOT NULL DEFAULT false,
    report_done           boolean NOT NULL DEFAULT true,
    transition_event_done boolean NOT NULL DEFAULT true,
    move_done             boolean NOT NULL DEFAULT true,
    audit_done            boolean NOT NULL DEFAULT true,
    comment_done          boolean NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS epic_transition_followups_pending
    ON epic_transition_followups (id)
    WHERE NOT (status_event_done AND report_done AND transition_event_done AND move_done AND audit_done AND comment_done);

CREATE TABLE IF NOT EXISTS epic_reopen_requests (
    epic          text NOT NULL,
    child         text NOT NULL,
    project       text NOT NULL,
    requested_at  timestamptz NOT NULL,
    PRIMARY KEY (epic, child)
);

ALTER TABLE project_reports ADD COLUMN IF NOT EXISTS lifecycle_key text;
CREATE UNIQUE INDEX IF NOT EXISTS project_reports_lifecycle_key
    ON project_reports (lifecycle_key) WHERE lifecycle_key IS NOT NULL;
