-- Canary reach (docs/v4/canary-reach.md): thresholds on the settings screen, pending merges and
-- merge-to-live measurements for the Dashboard.

CREATE TABLE IF NOT EXISTS canary_settings (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    minimum_age_s       integer NOT NULL DEFAULT 300,
    minimum_runs        integer NOT NULL DEFAULT 3,
    maximum_age_s       integer NOT NULL DEFAULT 900,
    updated_by          text NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now()
);

INSERT INTO canary_settings (id, minimum_age_s, minimum_runs, maximum_age_s)
VALUES (true, 300, 3, 900)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS canary_pending (
    build               text PRIMARY KEY,
    merged_at           timestamptz NOT NULL,
    noted_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS canary_merge_live (
    build               text PRIMARY KEY,
    merged_at           timestamptz NOT NULL,
    live_at             timestamptz NOT NULL,
    seconds             integer NOT NULL
);
CREATE INDEX IF NOT EXISTS canary_merge_live_live_at ON canary_merge_live (live_at DESC);
