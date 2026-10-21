-- Operational state retained long enough to make the SA recommendations meaningful.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS idle_since timestamptz;

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS credential_expires_at timestamptz;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS cooldown_since timestamptz;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS idle_since timestamptz;

CREATE TABLE IF NOT EXISTS sa_health (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    slot_pressure_since timestamptz
);
INSERT INTO sa_health (id) VALUES (true) ON CONFLICT (id) DO NOTHING;
