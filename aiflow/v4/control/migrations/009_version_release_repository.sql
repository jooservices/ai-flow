ALTER TABLE version_releases ADD COLUMN IF NOT EXISTS repository text NOT NULL DEFAULT '';
