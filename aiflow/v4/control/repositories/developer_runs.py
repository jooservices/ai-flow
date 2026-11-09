"""Queries over developer-run history."""
from __future__ import annotations

TIMEOUT_DAILY_SQL = """
SELECT (started_at AT TIME ZONE 'UTC')::date AS day,
       count(*) AS developer_runs,
       count(*) FILTER (WHERE outcome='timeout' OR error_kind='timeout') AS timeouts
FROM runs
WHERE step='dev'
  AND ended_at IS NOT NULL
  AND started_at >= ((now() AT TIME ZONE 'UTC')::date - %s::int)::timestamp AT TIME ZONE 'UTC'
  AND (%s OR project = ANY(%s))
GROUP BY 1 ORDER BY 1 DESC
"""

TIMEOUT_TICKET_SQL = """
SELECT ticket_key AS ticket, project,
       count(*) AS developer_runs,
       count(*) FILTER (WHERE outcome='timeout' OR error_kind='timeout') AS timeouts,
       max(started_at) FILTER (WHERE outcome='timeout' OR error_kind='timeout') AS latest_timeout
FROM runs
WHERE step='dev'
  AND ended_at IS NOT NULL
  AND started_at >= ((now() AT TIME ZONE 'UTC')::date - %s::int)::timestamp AT TIME ZONE 'UTC'
  AND (%s OR project = ANY(%s))
GROUP BY ticket_key,project
HAVING count(*) FILTER (WHERE outcome='timeout' OR error_kind='timeout') > 0
ORDER BY timeouts DESC, latest_timeout DESC, ticket
"""


def timeout_count(db, ticket_key: str) -> int:
    """Return ended developer runs for a ticket that timed out."""
    row = db.one(
        "SELECT count(*) AS count FROM runs WHERE ticket_key=%s AND step='dev'"
        " AND ended_at IS NOT NULL AND (outcome='timeout' OR error_kind='timeout')",
        (ticket_key,),
    )
    return int((row or {}).get("count") or 0)


def timeout_analytics(db, days: int, project_keys: list[str] | tuple[str, ...] | None):
    """Load daily and per-ticket timeout totals within the caller's project scope."""
    params = (days - 1, project_keys is None, list(project_keys or ()))
    return (db.all(TIMEOUT_DAILY_SQL, params), db.all(TIMEOUT_TICKET_SQL, params))


SILENT_DAILY_SQL = """
SELECT (started_at AT TIME ZONE 'UTC')::date AS day,
       count(*) AS developer_runs,
       count(*) FILTER (WHERE outcome='silent' OR error_kind IN ('silent', 'idle')) AS silent_runs
FROM runs
WHERE step='dev' AND ended_at IS NOT NULL
  AND started_at >= ((now() AT TIME ZONE 'UTC')::date - %s::int)::timestamp AT TIME ZONE 'UTC'
  AND (%s OR project = ANY(%s))
GROUP BY 1 ORDER BY 1 DESC
"""

SILENT_TICKET_SQL = """
SELECT ticket_key AS ticket, project,
       count(*) AS developer_runs,
       count(*) FILTER (WHERE outcome='silent' OR error_kind IN ('silent', 'idle')) AS silent_runs,
       max(started_at) FILTER (WHERE outcome='silent' OR error_kind IN ('silent', 'idle')) AS latest_silent
FROM runs
WHERE step='dev' AND ended_at IS NOT NULL
  AND started_at >= ((now() AT TIME ZONE 'UTC')::date - %s::int)::timestamp AT TIME ZONE 'UTC'
  AND (%s OR project = ANY(%s))
GROUP BY ticket_key,project
HAVING count(*) FILTER (WHERE outcome='silent' OR error_kind IN ('silent', 'idle')) > 0
ORDER BY silent_runs DESC, latest_silent DESC, ticket
"""

YIELD_DAILY_SQL = """
WITH window_runs AS (
  SELECT * FROM runs
  WHERE started_at >= ((now() AT TIME ZONE 'UTC')::date - %s::int)::timestamp AT TIME ZONE 'UTC'
    AND (%s OR project = ANY(%s))
), daily AS (
SELECT (started_at AT TIME ZONE 'UTC')::date AS day,
       count(*) FILTER (WHERE step='dev' AND ended_at IS NOT NULL) AS developer_runs,
       count(DISTINCT ticket_key) FILTER (WHERE step='verify' AND ended_at IS NOT NULL AND outcome='merged') AS merged,
       count(*) FILTER (WHERE step='verify' AND ended_at IS NOT NULL AND outcome='rework') AS returns,
       coalesce(sum(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)) FILTER (WHERE ended_at IS NOT NULL), 0) AS tokens
FROM window_runs
GROUP BY 1 ORDER BY 1 DESC
)
SELECT daily.*,
       (SELECT count(DISTINCT ticket_key) FROM window_runs
        WHERE step='verify' AND ended_at IS NOT NULL AND outcome='merged') AS window_merged
FROM daily
"""


def silent_run_analytics(db, days: int, project_keys: list[str] | tuple[str, ...] | None):
    """Load daily and per-ticket silent-run totals within the caller's project scope."""
    params = (days - 1, project_keys is None, list(project_keys or ()))
    return (db.all(SILENT_DAILY_SQL, params), db.all(SILENT_TICKET_SQL, params))


def yield_analytics(db, days: int, project_keys: list[str] | tuple[str, ...] | None):
    """Load daily completed developer runs, merged tickets, verify returns and consumed tokens."""
    params = (days - 1, project_keys is None, list(project_keys or ()))
    return db.all(YIELD_DAILY_SQL, params)
