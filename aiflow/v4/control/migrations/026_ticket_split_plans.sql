CREATE TABLE IF NOT EXISTS ticket_split_plans (
    parent_key      text PRIMARY KEY,
    project         text NOT NULL,
    estimate        jsonb NOT NULL,
    target_minutes  integer NOT NULL,
    plan            jsonb NOT NULL,
    child_keys      text[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'pending',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
