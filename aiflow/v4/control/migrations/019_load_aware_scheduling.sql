-- Per-machine limits and shared host limits for deterministic load-aware work offers.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS load_limits jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS load_state jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS machine_host_load_limits (
    hostname            text PRIMARY KEY,
    cpu_load_per_cpu    double precision NOT NULL DEFAULT 0.85 CHECK (cpu_load_per_cpu > 0 AND cpu_load_per_cpu <= 8),
    state               jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
