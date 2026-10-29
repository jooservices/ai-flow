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
