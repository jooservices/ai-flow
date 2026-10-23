ALTER TABLE managed_ai_accounts ADD COLUMN IF NOT EXISTS sign_in_request_id text NOT NULL DEFAULT '';
ALTER TABLE managed_ai_accounts ADD COLUMN IF NOT EXISTS sign_in_status text NOT NULL DEFAULT 'idle';
ALTER TABLE managed_ai_accounts ADD COLUMN IF NOT EXISTS sign_in_error text NOT NULL DEFAULT '';

ALTER TABLE managed_ai_accounts DROP CONSTRAINT IF EXISTS managed_ai_accounts_sign_in_status_check;
ALTER TABLE managed_ai_accounts ADD CONSTRAINT managed_ai_accounts_sign_in_status_check
    CHECK (sign_in_status IN ('idle', 'queued', 'starting', 'waiting', 'completed', 'failed'));

-- Earlier screen builds displayed a locally generated code that no provider could authorize.
-- Do not mistake it for a real authorization session after upgrading.
UPDATE managed_ai_accounts SET sign_in_url='', sign_in_code='', sign_in_expires_at=NULL,
    sign_in_status='failed', sign_in_error='Start a new provider sign-in from the Machines and AI screen.'
WHERE sign_in_code<>'' AND sign_in_status='idle';
