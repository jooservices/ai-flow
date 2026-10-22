-- Version-scoped release tracking and idempotency state.
CREATE TABLE IF NOT EXISTS version_releases (
    project             text NOT NULL,
    version_id          text NOT NULL,
    version_name        text NOT NULL DEFAULT '',
    tracker_key         text NOT NULL DEFAULT '',
    status              text NOT NULL DEFAULT 'pending', -- pending | blocked | checking | failed | released
    gate_detail         text NOT NULL DEFAULT '',
    ticket_snapshot     jsonb NOT NULL DEFAULT '[]'::jsonb,
    checked_develop_sha text NOT NULL DEFAULT '',
    master_sha          text NOT NULL DEFAULT '',
    tag                 text NOT NULL DEFAULT '',
    github_release_url  text NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, version_id)
);
