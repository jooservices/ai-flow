"""Persistence queries for run history and role-health scoring."""
from __future__ import annotations


ROLE_HEALTH_RUNS_SQL = """
WITH recent AS (
  SELECT id, ticket_key, step, account_id, outcome, error_kind, summary, details, pr_url, node, started_at, cost_usd,
         row_number() OVER (PARTITION BY account_id ORDER BY started_at DESC) AS n
  FROM runs
  WHERE status <> 'running' AND outcome <> '' AND step = ANY(%(steps)s::text[])
    AND (%(account)s::text = '' OR account_id = %(account)s::text)
)
SELECT r.*, CASE WHEN r.step = 'dev' AND r.outcome = 'done' THEN
  (SELECT v.outcome FROM runs v WHERE v.ticket_key = r.ticket_key AND v.step IN ('verify', 'qa')
     AND v.started_at > r.started_at AND v.outcome IN ('merged', 'human', 'rework')
   ORDER BY v.started_at LIMIT 1) END AS decision,
  CASE WHEN r.step = 'verify' THEN coalesce(
  (SELECT p.details->>'no_verdict' = 'true' FROM runs p WHERE p.ticket_key = r.ticket_key
     AND p.account_id = r.account_id AND p.step = 'verify' AND p.status <> 'running' AND p.started_at < r.started_at
     AND p.started_at > coalesce((SELECT max(d.started_at) FROM runs d WHERE d.ticket_key = r.ticket_key
                                  AND d.step = 'dev' AND d.outcome = 'done' AND d.started_at < r.started_at),
                                 '-infinity'::timestamptz)
   ORDER BY p.started_at DESC LIMIT 1), false) END AS prev_no_verdict
FROM recent r WHERE r.n <= %(limit)s ORDER BY r.started_at DESC
"""

TICKET_STUCK_STREAKS_SQL = """
WITH recent AS (
  SELECT ticket_key, outcome, details, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM recent l
  WHERE l.outcome IN ('failed', 'timeout', 'lost', 'no_changes')
    AND coalesce(l.details->>'no_verdict' = 'true', false) = false
    AND NOT EXISTS (SELECT 1 FROM recent g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND (g.outcome NOT IN ('failed', 'timeout', 'lost', 'no_changes')
                         OR coalesce(g.details->>'no_verdict' = 'true', false)))
  GROUP BY l.ticket_key
)
SELECT ticket_key, fails FROM streak
WHERE last_end > now() - make_interval(mins => LEAST(30, (2 ^ LEAST(fails, 5))::int))
"""


class RunsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def role_health_history(self, steps: list[str], account: str = "", limit: int = 500) -> list[dict]:
        """Finished runs with the prior verdict marker needed by role-health policy."""
        return self.db.all(ROLE_HEALTH_RUNS_SQL, {"steps": steps, "account": account, "limit": limit})

    def ticket_stuck_streaks(self, step: str) -> list[dict]:
        """Recent generic failure streaks; no-verdict verification runs break rather than extend the streak."""
        return self.db.all(TICKET_STUCK_STREAKS_SQL, {"step": step})
