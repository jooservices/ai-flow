-- Project assignments made in the console are distinct from legacy file assignments before inventory import.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS managed_projects text[] NOT NULL DEFAULT '{}';
