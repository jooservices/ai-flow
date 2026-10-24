-- Runtime-setting inventory is metadata only; secret values remain in permission-restricted env files.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS runtime_settings jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS runtime_settings_public_key text NOT NULL DEFAULT '';
