-- Owner managed alert channels, routing preferences, delivery retries and scheduled report clocks.
CREATE TABLE IF NOT EXISTS notification_channels (
    id                    bigserial PRIMARY KEY,
    name                  text NOT NULL,
    kind                  text NOT NULL CHECK (kind IN ('email','telegram','slack','teams','webhook')),
    enabled               boolean NOT NULL DEFAULT true,
    config                jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_delivery_status  text NOT NULL DEFAULT 'never',
    last_delivery_at      timestamptz,
    last_delivery_error   text NOT NULL DEFAULT '',
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notification_preferences (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    routes      jsonb NOT NULL DEFAULT '{}'::jsonb,
    quiet_start text NOT NULL DEFAULT '',
    quiet_end   text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO notification_preferences (id) VALUES (true) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id              bigserial PRIMARY KEY,
    channel_id      bigint REFERENCES notification_channels(id) ON DELETE SET NULL,
    channel_name    text NOT NULL,
    channel_kind    text NOT NULL,
    alert_kind      text NOT NULL,
    title           text NOT NULL,
    body            text NOT NULL,
    link            text NOT NULL DEFAULT '',
    project         text NOT NULL DEFAULT '',
    ticket          text NOT NULL DEFAULT '',
    needed          text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed')),
    attempts        integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 4,
    next_attempt_at timestamptz,
    last_error      text NOT NULL DEFAULT '',
    is_test         boolean NOT NULL DEFAULT false,
    incident_id     bigint REFERENCES incidents(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz
);
CREATE INDEX IF NOT EXISTS notification_deliveries_due ON notification_deliveries (next_attempt_at, id)
    WHERE status='pending';
CREATE INDEX IF NOT EXISTS notification_deliveries_channel ON notification_deliveries (channel_id, id DESC);

CREATE TABLE IF NOT EXISTS notification_schedules (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    last_daily_date     date,
    last_weekly_monday  date
);
INSERT INTO notification_schedules (id) VALUES (true) ON CONFLICT (id) DO NOTHING;
