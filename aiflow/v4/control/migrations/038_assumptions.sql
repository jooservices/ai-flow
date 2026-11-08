-- Track conservative assumptions made by intake and dev on structure, naming, scope, metrics, defaults and tools.
CREATE TABLE IF NOT EXISTS assumptions (
    id                      bigserial PRIMARY KEY,
    ticket_key              text NOT NULL,
    project                 text NOT NULL,
    run_id                  text NOT NULL DEFAULT '',
    step                    text NOT NULL DEFAULT 'intake',
    assumption              text NOT NULL,
    why                     text NOT NULL DEFAULT '',
    source                  text NOT NULL DEFAULT 'flow',
    created_at              timestamptz NOT NULL DEFAULT now(),
    contradicted            boolean NOT NULL DEFAULT false,
    contradicted_at         timestamptz,
    contradicted_by_run_id  text NOT NULL DEFAULT '',
    contradiction_reason    text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS assumptions_created_day
    ON assumptions (created_at DESC);

CREATE INDEX IF NOT EXISTS assumptions_ticket_key
    ON assumptions (ticket_key);
