-- Deferred machine-bound ticket criteria are checked by the configured post-merge release run.
CREATE TABLE IF NOT EXISTS environment_post_merge_checks (
    ticket_key text PRIMARY KEY,
    project text NOT NULL,
    criteria jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'pending',       -- pending | passed | failed
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    release_ticket text NOT NULL DEFAULT '',
    run_id text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    checked_at timestamptz
);
CREATE INDEX IF NOT EXISTS environment_post_merge_project ON environment_post_merge_checks (project, status, created_at);
