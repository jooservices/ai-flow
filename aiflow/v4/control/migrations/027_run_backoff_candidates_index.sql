-- Worker polls only need failure streaks for tickets that failed recently. Find that sparse set
-- without scanning every completed run for the workflow step on each candidate request.
CREATE INDEX IF NOT EXISTS runs_backoff_candidates
    ON runs (step, outcome, started_at DESC, ticket_key)
    WHERE status <> 'running';
