-- Project lifecycle and immutable report snapshots. Study cases are also indexed in memory for search.

ALTER TABLE memory DROP CONSTRAINT IF EXISTS memory_kind_check;
ALTER TABLE memory ADD CONSTRAINT memory_kind_check
    CHECK (kind IN ('ticket', 'run_summary', 'finding', 'answer', 'study_case'));

CREATE TABLE IF NOT EXISTS project_lifecycle (
    project         text PRIMARY KEY,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_at       timestamptz,
    closed_by       text NOT NULL DEFAULT '',
    version         integer NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS project_reports (
    id              bigserial PRIMARY KEY,
    project         text NOT NULL,
    scope           text NOT NULL CHECK (scope IN ('project', 'epic')),
    subject         text NOT NULL,
    version         integer NOT NULL CHECK (version > 0),
    report          jsonb NOT NULL,
    markdown        text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_channels   text[] NOT NULL DEFAULT '{}',
    UNIQUE (project, scope, subject, version)
);
CREATE INDEX IF NOT EXISTS project_reports_created ON project_reports (project, created_at DESC);
