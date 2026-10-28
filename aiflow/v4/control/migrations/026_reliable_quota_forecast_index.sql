-- Worker candidate polls need quota forecasts only when they are reliable enough to affect routing.
-- Keep the no-forecast fleet path from scanning and materializing every account on every poll.
CREATE INDEX IF NOT EXISTS accounts_reliable_quota_forecast
    ON accounts (id)
    WHERE quota_forecast->>'source' = 'reported'
       OR quota_forecast->>'confidence' IN ('medium', 'high');
