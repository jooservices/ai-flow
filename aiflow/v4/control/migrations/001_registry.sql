-- AI Flow v4 registry: nodes, accounts, tickets (Jira mirror), runs, events, write audit.

CREATE TABLE IF NOT EXISTS nodes (
    name            text PRIMARY KEY,
    ip              text NOT NULL DEFAULT '',
    role            text NOT NULL DEFAULT 'worker',
    status          text NOT NULL DEFAULT 'joined',      -- joined | online | draining | offline
    token_hash      text NOT NULL DEFAULT '',
    slots_total     integer NOT NULL DEFAULT 1,
    slots_used      integer NOT NULL DEFAULT 0,
    release_version text NOT NULL DEFAULT '',
    tools           jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics         jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_heartbeat  timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounts (
    id              text PRIMARY KEY,
    provider        text NOT NULL,
    node            text NOT NULL DEFAULT '',
    tier            text NOT NULL DEFAULT 'middle',
    roles           text[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'unknown',     -- idle | working | cooldown | signed_out | error | unknown
    worker_state    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- reported by the node: pid, run, since
    quota           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- provider data: utilization per window, resets_at
    cooldown_until  timestamptz,
    last_error      text NOT NULL DEFAULT '',
    last_probe_at   timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tickets (
    key             text PRIMARY KEY,
    project         text NOT NULL,
    status          text NOT NULL,
    type            text NOT NULL DEFAULT 'Task',
    priority        text NOT NULL DEFAULT 'Medium',
    labels          text[] NOT NULL DEFAULT '{}',
    summary         text NOT NULL DEFAULT '',
    description     text NOT NULL DEFAULT '',
    parent_key      text,
    blocked_by      text[] NOT NULL DEFAULT '{}',
    fields          jsonb NOT NULL DEFAULT '{}'::jsonb,
    jira_created    timestamptz,
    jira_updated    timestamptz,
    synced_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tickets_project_status ON tickets (project, status);

CREATE TABLE IF NOT EXISTS runs (
    id              uuid PRIMARY KEY,
    ticket_key      text NOT NULL,
    project         text NOT NULL,
    step            text NOT NULL,                       -- dev | verify | intake | master | qa
    account_id      text NOT NULL,
    provider        text NOT NULL DEFAULT '',
    model           text NOT NULL DEFAULT '',
    node            text NOT NULL,
    fence           bigint NOT NULL,
    token_hash      text NOT NULL DEFAULT '',            -- run token for policy hooks
    status          text NOT NULL DEFAULT 'running',     -- running | finished | released | lost
    outcome         text NOT NULL DEFAULT '',            -- done | blocked | failed | quota | timeout | lost
    error_kind      text NOT NULL DEFAULT '',
    reset_at        timestamptz,
    started_at      timestamptz NOT NULL DEFAULT now(),
    heartbeat_at    timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    progress        jsonb NOT NULL DEFAULT '{}'::jsonb,
    tokens_in       bigint NOT NULL DEFAULT 0,
    tokens_out      bigint NOT NULL DEFAULT 0,
    cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
    branch          text NOT NULL DEFAULT '',
    pr_url          text NOT NULL DEFAULT '',
    head_sha        text NOT NULL DEFAULT '',
    summary         text NOT NULL DEFAULT '',
    details         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS runs_ticket ON runs (ticket_key, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, heartbeat_at);

CREATE TABLE IF NOT EXISTS events (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    type            text NOT NULL,
    ticket          text NOT NULL DEFAULT '',
    project         text NOT NULL DEFAULT '',
    step            text NOT NULL DEFAULT '',
    run_id          text NOT NULL DEFAULT '',
    fence           bigint,
    account         text NOT NULL DEFAULT '',
    provider        text NOT NULL DEFAULT '',
    model           text NOT NULL DEFAULT '',
    node            text NOT NULL DEFAULT '',
    duration_ms     bigint,
    outcome         text NOT NULL DEFAULT '',
    reason          text NOT NULL DEFAULT '',
    data            jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS events_ticket ON events (ticket, ts DESC);

CREATE TABLE IF NOT EXISTS write_audit (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    target          text NOT NULL,                       -- jira | github
    action          text NOT NULL,
    ticket          text NOT NULL DEFAULT '',
    run_id          text NOT NULL DEFAULT '',
    fence           bigint,
    actor           text NOT NULL DEFAULT '',            -- account or "control"
    accepted        boolean NOT NULL,
    result          text NOT NULL DEFAULT '',
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS write_audit_ts ON write_audit (ts DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id              bigserial PRIMARY KEY,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    severity        text NOT NULL DEFAULT 'warning',     -- info | warning | critical
    kind            text NOT NULL,                       -- node_down | account_quota | run_stalled | ...
    subject         text NOT NULL,                       -- node name, account id, ticket key
    title           text NOT NULL,
    detail          text NOT NULL DEFAULT '',
    notified        boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS incidents_open ON incidents (kind, subject) WHERE closed_at IS NULL;
