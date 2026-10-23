-- Jira can be temporarily unavailable during a rollback. Keep enough information to retry independently of
-- opening the incident (which happens only once) and to recover if Jira created the issue before a local error.
CREATE TABLE IF NOT EXISTS canary_flow_bugs (
    target           text PRIMARY KEY,
    node             text NOT NULL,
    previous         text NOT NULL,
    suspected_commit text NOT NULL,
    project          text NOT NULL,
    summary          text NOT NULL,
    description      text NOT NULL,
    dedupe_label     text NOT NULL UNIQUE,
    ticket           text NOT NULL DEFAULT '',
    last_error       text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
