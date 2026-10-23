-- Each shared sign-in attempt on a machine gets its own request id, so a failed attempt can be retried.
ALTER TABLE account_machines ADD COLUMN IF NOT EXISTS signin_attempt integer NOT NULL DEFAULT 1;
