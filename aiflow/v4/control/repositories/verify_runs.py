"""Persistence for verification runs that end without a verdict (policy: control/verify_retry.py)."""
from __future__ import annotations

# A failed verify run waits before the next try (the shared backoff), except a no-verdict run, which is retried
# on another account at once.
VERIFY_BACKOFF_SQL = """
WITH last AS (
  SELECT ticket_key, outcome, ended_at,
         coalesce(details->>'no_verdict' = 'true', false) AS no_verdict,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM last l
  WHERE l.outcome IN ('failed', 'timeout', 'lost', 'no_changes') AND NOT l.no_verdict
    AND NOT EXISTS (SELECT 1 FROM last g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND (g.no_verdict OR g.outcome NOT IN ('failed', 'timeout', 'lost', 'no_changes')))
  GROUP BY l.ticket_key
)
SELECT ticket_key, fails FROM streak
WHERE last_end > now() - make_interval(mins => LEAST(30, (2 ^ LEAST(fails, 5))::int))
"""

_ROUND_SQL = """
SELECT account_id, outcome, started_at, ended_at, coalesce(details->>'no_verdict' = 'true', false) AS no_verdict
FROM runs WHERE ticket_key = %s AND step = 'verify' AND status <> 'running'
  AND started_at > coalesce((SELECT max(started_at) FROM runs WHERE ticket_key = %s AND step = 'dev'
                             AND outcome = 'done'), '-infinity'::timestamptz)
ORDER BY started_at DESC
"""

class VerifyRunsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def cooling_tickets(self, step: str = "verify") -> set[str]:
        """Tickets whose failed `step` runs still wait out the shared backoff (no-verdict runs excluded)."""
        return {r["ticket_key"] for r in self.db.all(VERIFY_BACKOFF_SQL, {"step": step})}

    def round_runs(self, ticket_key: str) -> list[dict]:
        """Finished verify runs of the current development round, newest first."""
        return self.db.all(_ROUND_SQL, (ticket_key, ticket_key))

    def last_dev_account(self, ticket_key: str) -> str:
        row = self.db.one("SELECT account_id FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='done'"
                          " ORDER BY started_at DESC LIMIT 1", (ticket_key,))
        return (row or {}).get("account_id", "")

    def latest_dev_with_pr(self, ticket_key: str) -> dict | None:
        return self.db.one("SELECT account_id, pr_url, details FROM runs WHERE ticket_key=%s AND step='dev'"
                           " AND outcome='done' ORDER BY started_at DESC LIMIT 1", (ticket_key,))

    def now(self):
        return self.db.one("SELECT now() AS t")["t"]
