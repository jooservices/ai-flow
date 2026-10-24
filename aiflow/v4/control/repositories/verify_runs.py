"""Persistence for verification runs that end without a verdict (policy: control/verify_retry.py)."""
from __future__ import annotations

# A failed verify run waits before the next try (the shared backoff), except a no-verdict run, which is retried
# on another account at once.
VERIFY_BACKOFF_SQL = """
WITH last AS (
  SELECT ticket_key, outcome, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
    AND NOT coalesce(details->>'no_verdict' = 'true', false)
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM last l
  WHERE l.outcome IN ('failed', 'timeout', 'lost', 'no_changes')
    AND NOT EXISTS (SELECT 1 FROM last g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND g.outcome NOT IN ('failed', 'timeout', 'lost', 'no_changes'))
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

_SLOW_SQL = """
SELECT t.key, t.project, t.summary, coalesce(d.ended_at, t.jira_updated, t.synced_at) AS since
FROM tickets t LEFT JOIN LATERAL (SELECT ended_at FROM runs WHERE ticket_key=t.key AND step='dev'
  AND outcome='done' ORDER BY started_at DESC LIMIT 1) d ON true
WHERE t.status='In Verify' AND lower(coalesce(t.type,'')) <> 'epic'
  AND NOT ('aiflow-hold' = ANY(t.labels))
  AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.status='running')
  AND coalesce(d.ended_at, t.jira_updated, t.synced_at) <= now() - %s * interval '1 minute'
ORDER BY since
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

    def tickets_in_verify(self) -> list[dict]:
        return self.db.all("SELECT key, labels, type FROM tickets WHERE status='In Verify'"
                           " AND lower(coalesce(type,'')) <> 'epic'")

    def accepting_nodes(self) -> set[str]:
        return {r["name"] for r in self.db.all("SELECT name FROM nodes WHERE removed_at IS NULL AND accepting_work")}

    def open_incident_subjects(self, kind: str) -> list[str]:
        return [r["subject"] for r in self.db.all(
            "SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (kind,))]

    def waiting_tickets(self, minutes: int) -> list[dict]:
        """Tickets in verification with no running work for longer than `minutes`."""
        return self.db.all(_SLOW_SQL, (minutes,))

    def now(self):
        return self.db.one("SELECT now() AS t")["t"]
