-- Durable root-cause counters and normalized child rows. This migration deliberately does not scan runs or
-- rewrite run_ids; existing trackers seed occurrence_count from the ids already stored on the tracker.
ALTER TABLE flow_bug_tracker
    ADD COLUMN IF NOT EXISTS occurrence_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS first_seen_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_seen_at timestamptz,
    ADD COLUMN IF NOT EXISTS tokens_wasted bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cost_wasted numeric(14,6) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS breaker_active boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS bug_status text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS fixed_at timestamptz,
    ADD COLUMN IF NOT EXISTS watch_until timestamptz,
    ADD COLUMN IF NOT EXISTS regression_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS blocked_step text NOT NULL DEFAULT '';

UPDATE flow_bug_tracker
   SET occurrence_count = GREATEST(occurrence_count, cardinality(run_ids))
 WHERE occurrence_count < cardinality(run_ids);

CREATE TABLE IF NOT EXISTS flow_bug_signature_members (
    signature       text NOT NULL REFERENCES flow_bug_tracker(signature) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('ticket', 'step', 'provider', 'account')),
    value           text NOT NULL,
    first_seen_at   timestamptz NOT NULL,
    last_seen_at    timestamptz NOT NULL,
    occurrences     bigint NOT NULL DEFAULT 0 CHECK (occurrences >= 0),
    PRIMARY KEY (signature, kind, value)
);
CREATE INDEX IF NOT EXISTS flow_bug_signature_members_last_seen
    ON flow_bug_signature_members (last_seen_at);

CREATE TABLE IF NOT EXISTS flow_bug_blocked_tickets (
    signature       text NOT NULL REFERENCES flow_bug_tracker(signature) ON DELETE CASCADE,
    ticket_key      text NOT NULL,
    PRIMARY KEY (signature, ticket_key)
);
CREATE INDEX IF NOT EXISTS flow_bug_blocked_tickets_ticket_key
    ON flow_bug_blocked_tickets (ticket_key);

CREATE TABLE IF NOT EXISTS flow_bug_sample_messages (
    signature       text NOT NULL REFERENCES flow_bug_tracker(signature) ON DELETE CASCADE,
    position        smallint NOT NULL CHECK (position BETWEEN 1 AND 3),
    message         text NOT NULL,
    PRIMARY KEY (signature, position)
);

CREATE INDEX IF NOT EXISTS runs_failure_signature_recent
    ON runs ((coalesce(ended_at,started_at)) DESC) WHERE status<>'running' AND outcome IN ('failed','timeout');
