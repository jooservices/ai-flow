-- Machines, AI accounts, roles and worker counts live in this database (docs/v4/inventory-in-database.md).
-- `control_imports` records the one-time import of the control repository's accounts.yaml and nodes/*.yaml.
CREATE TABLE IF NOT EXISTS control_imports (
    name         text PRIMARY KEY,
    imported_at  timestamptz NOT NULL DEFAULT now(),
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);
-- A label the owner gives an account on the Machines and AI screen; the account id stays generated.
ALTER TABLE managed_ai_accounts ADD COLUMN IF NOT EXISTS name text NOT NULL DEFAULT '';
