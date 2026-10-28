-- Scale proof (docs/v4/scale-proof.md): every run of the 100 projects / 50 machines / 200 sessions scenario is
-- attached to a release, and every finding becomes one ticket (one row per finding signature).
CREATE TABLE IF NOT EXISTS scale_proof_runs (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    release     text NOT NULL DEFAULT '',
    passed      boolean NOT NULL,
    plan_digest text NOT NULL DEFAULT '',
    source      text NOT NULL DEFAULT '',
    report      jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS scale_proof_runs_release ON scale_proof_runs (release, id DESC);

CREATE TABLE IF NOT EXISTS scale_proof_findings (
    signature    text PRIMARY KEY,
    kind         text NOT NULL,
    title        text NOT NULL,
    detail       text NOT NULL DEFAULT '',
    ticket       text NOT NULL DEFAULT '',
    first_run_id bigint NOT NULL,
    last_run_id  bigint NOT NULL,
    occurrences  integer NOT NULL DEFAULT 1,
    last_error   text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
