-- Each claim gets a unique fence so a reclaimed turn cannot accept an older worker's result.
ALTER TABLE ba_chat_jobs ADD COLUMN IF NOT EXISTS claim_token uuid;
