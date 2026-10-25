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
  CASE WHEN r.step = 'verify' THEN
  (SELECT coalesce(p.details->>'no_verdict' = 'true', false) FROM runs p WHERE p.ticket_key = r.ticket_key
     AND p.account_id = r.account_id AND p.step = 'verify' AND p.status <> 'running' AND p.started_at < r.started_at
   ORDER BY p.started_at DESC LIMIT 1) END AS prev_no_verdict
FROM recent r WHERE r.n <= %(limit)s ORDER BY r.started_at DESC
"""


class RunsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def role_health_history(self, steps: list[str], account: str = "", limit: int = 500) -> list[dict]:
        """Finished runs with the prior verdict marker needed by role-health policy."""
        return self.db.all(ROLE_HEALTH_RUNS_SQL, {"steps": steps, "account": account, "limit": limit})
