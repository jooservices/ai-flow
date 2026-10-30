"""Constants of the work feature: ticket priorities, the retry back-off queries and the clock they share."""
from __future__ import annotations

from datetime import datetime, timezone

PRIORITY = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# A ticket whose recent dev runs failed waits before the next try: 2, 4, 8, 16 then 30 minutes after the last
# failure (consecutive failures since the last run that produced work), so a broken ticket cannot spin.
BACKOFF_SQL = """
WITH candidates AS (
  SELECT DISTINCT ticket_key FROM runs WHERE step = %(step)s AND status <> 'running'
    AND started_at > now() - interval '1 day'
    AND outcome IN ('failed', 'timeout', 'lost', 'no_changes')
), last AS (
  SELECT r.ticket_key, r.outcome, r.ended_at,
         row_number() OVER (PARTITION BY r.ticket_key ORDER BY r.started_at DESC) AS n
  FROM candidates c JOIN runs r ON r.ticket_key = c.ticket_key
  WHERE r.step = %(step)s AND r.status <> 'running' AND r.started_at > now() - interval '1 day'
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
# An Intake ticket whose last intake run failed waits 10 minutes before the next refinement try (the same for the
# impact assessment of a change request).
INTAKE_BACKOFF_SQL = """
SELECT DISTINCT ON (ticket_key) ticket_key, outcome, ended_at FROM runs
WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
ORDER BY ticket_key, started_at DESC
"""
