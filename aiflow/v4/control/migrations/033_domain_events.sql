-- Domain events and their outbox deliveries (docs/v4/event-architecture.md). An event row is written in the same
-- transaction as the change it describes; one delivery row per outbox listener says whether it ran.
CREATE TABLE IF NOT EXISTS domain_events (
  seq            bigserial PRIMARY KEY,           -- global order; deliveries of one aggregate run in this order
  id             text NOT NULL UNIQUE,
  type           text NOT NULL,
  version        int NOT NULL DEFAULT 1,
  occurred_at    timestamptz NOT NULL,
  aggregate      text NOT NULL,
  aggregate_id   text NOT NULL,
  correlation_id text NOT NULL,
  causation_id   text NOT NULL DEFAULT '',
  payload        jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS domain_events_aggregate ON domain_events (aggregate, aggregate_id, seq);
CREATE INDEX IF NOT EXISTS domain_events_correlation ON domain_events (correlation_id);

CREATE TABLE IF NOT EXISTS domain_event_deliveries (
  event_id        text NOT NULL REFERENCES domain_events (id) ON DELETE CASCADE,
  listener        text NOT NULL,
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dead')),
  attempts        int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text NOT NULL DEFAULT '',
  delivered_at    timestamptz,
  PRIMARY KEY (event_id, listener)
);
CREATE INDEX IF NOT EXISTS domain_event_deliveries_due ON domain_event_deliveries (next_attempt_at) WHERE status = 'pending';
