-- The installation ownership capability can create the first Administrator only once.
ALTER TABLE setup_wizard_state ADD COLUMN IF NOT EXISTS ownership_claimed_at timestamptz;
