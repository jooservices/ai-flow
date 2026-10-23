-- The watchdog refreshes this forecast flag; candidate polls can route without rereading weeks of history.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS quota_at_risk boolean NOT NULL DEFAULT false;
