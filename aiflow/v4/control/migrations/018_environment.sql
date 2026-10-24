-- Tickets carry requirements only; servers, hosts and access live in the project configuration (docs/v4/environment.md).

-- A developer run that needs an environment item that is not provided reports it by name (a name from the project
-- configuration). One open row per ticket and item; the flow doctor provides the item or one environment ticket is raised.
CREATE TABLE IF NOT EXISTS environment_reports (
    id bigserial PRIMARY KEY,
    ticket_key text NOT NULL,
    project text NOT NULL DEFAULT '',
    item text NOT NULL,
    machine text NOT NULL DEFAULT '',
    run_id text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    fingerprint text NOT NULL DEFAULT '',
    environment_ticket text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution text NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS environment_reports_open ON environment_reports (ticket_key, item) WHERE resolved_at IS NULL;

-- What the intake AI removed from a ticket (the wording is kept here, never in the ticket) and the environment needs
-- it found behind that wording.
CREATE TABLE IF NOT EXISTS environment_removals (
    id bigserial PRIMARY KEY,
    ticket_key text NOT NULL,
    project text NOT NULL DEFAULT '',
    run_id text NOT NULL DEFAULT '',
    removed jsonb NOT NULL DEFAULT '[]'::jsonb,
    needs jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS environment_removals_ticket ON environment_removals (ticket_key);

-- A real need behind removed wording ("tests need a seeded site") is one environment ticket per project and need.
CREATE TABLE IF NOT EXISTS environment_needs (
    id bigserial PRIMARY KEY,
    project text NOT NULL,
    need_key text NOT NULL,
    need text NOT NULL,
    environment_ticket text NOT NULL,
    linked_tickets text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project, need_key)
);

-- The scan of the open tickets of every project: who breaks the rule and what was done about it.
CREATE TABLE IF NOT EXISTS environment_scans (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL DEFAULT 'system',
    scanned integer NOT NULL DEFAULT 0,
    offenders jsonb NOT NULL DEFAULT '[]'::jsonb
);
