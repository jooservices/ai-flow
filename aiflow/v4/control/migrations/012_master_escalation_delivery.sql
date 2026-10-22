-- Track owner alert delivery per channel so a transient channel failure is retried on later master cycles.
ALTER TABLE master_escalations ADD COLUMN IF NOT EXISTS pending_channels text[];
ALTER TABLE master_escalations ADD COLUMN IF NOT EXISTS delivered_channels text[] NOT NULL DEFAULT '{}'::text[];
ALTER TABLE master_escalations ADD COLUMN IF NOT EXISTS intent text NOT NULL DEFAULT '';
