-- Tools missing on a machine (docs/v4/missing-tools.md): tickets wait for a capable machine instead of asking the owner.
CREATE TABLE IF NOT EXISTS tool_waits (
    id bigserial PRIMARY KEY,
    ticket_key text NOT NULL,
    project text NOT NULL DEFAULT '',
    requirement text NOT NULL,
    machine text NOT NULL DEFAULT '',
    run_id text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution text NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS tool_waits_open ON tool_waits (ticket_key, requirement) WHERE resolved_at IS NULL;

-- One row per requirement that no machine can supply: `since` starts the 24 hour clock, `maintenance_key` is the
-- single maintenance ticket opened for it.
CREATE TABLE IF NOT EXISTS tooling_gaps (
    requirement text PRIMARY KEY,
    since timestamptz NOT NULL DEFAULT now(),
    maintenance_key text NOT NULL DEFAULT '',
    maintenance_opened_at timestamptz
);

-- The project's check commands, run once on a clean checkout per machine (and again when the commands change).
CREATE TABLE IF NOT EXISTS project_check_probes (
    project text NOT NULL,
    node text NOT NULL,
    signature text NOT NULL DEFAULT '',
    checked_at timestamptz NOT NULL DEFAULT now(),
    results jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (project, node)
);
