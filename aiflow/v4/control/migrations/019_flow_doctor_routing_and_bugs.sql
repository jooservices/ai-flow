-- Flow doctor: routes a ticket must not use again, and operator-intervention sessions that own one Bug each.

-- When the doctor moves a ticket to another account or machine, the failed route is excluded for a while; the
-- claim path (`eligible`) honours the rows, so the failed route cannot be selected again.
CREATE TABLE IF NOT EXISTS flow_doctor_route_exclusions (
    id              bigserial PRIMARY KEY,
    ticket_key      text NOT NULL,
    step            text NOT NULL DEFAULT '',
    account_id      text NOT NULL DEFAULT '',
    node            text NOT NULL DEFAULT '',
    reason          text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    UNIQUE (ticket_key, step, account_id, node)
);
CREATE INDEX IF NOT EXISTS flow_doctor_route_exclusions_ticket ON flow_doctor_route_exclusions (ticket_key);

-- One session = the actions of one actor and source with gaps under 30 minutes. A session owns one Bug; later
-- actions of the same session are attached to that Bug, whichever doctor pass sees them.
CREATE TABLE IF NOT EXISTS operator_intervention_sessions (
    id              bigserial PRIMARY KEY,
    actor           text NOT NULL,
    source          text NOT NULL,
    started_at      timestamptz NOT NULL,
    last_event_at   timestamptz NOT NULL,
    event_count     integer NOT NULL DEFAULT 0,
    reported_count  integer NOT NULL DEFAULT 0,
    bug_ticket_key  text NOT NULL DEFAULT '',
    in_intake       boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS operator_intervention_sessions_actor ON operator_intervention_sessions (actor, source, last_event_at DESC);
ALTER TABLE operator_interventions ADD COLUMN IF NOT EXISTS session_id bigint REFERENCES operator_intervention_sessions(id);
CREATE INDEX IF NOT EXISTS operator_interventions_session ON operator_interventions (session_id);
