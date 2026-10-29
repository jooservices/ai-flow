-- Preserve the imported machine profile in the registry before legacy nodes/*.yaml files are removed.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS profile jsonb NOT NULL DEFAULT '{}'::jsonb;
