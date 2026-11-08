"""Queries for first observed pipeline results."""
from __future__ import annotations


FIRST_PIPELINE_FAILURES_SQL = """
WITH first_checked AS (
  SELECT DISTINCT ON (r.pr_url) r.ticket_key, r.project, r.pr_url, r.started_at, r.details
  FROM runs r
  WHERE r.step = 'verify' AND r.pr_url <> ''
    AND CASE WHEN jsonb_typeof(r.details->'ci') = 'array'
             THEN jsonb_array_length(r.details->'ci') > 0 ELSE false END
    AND (%(project_keys_is_null)s OR r.project = ANY(%(project_keys)s))
  ORDER BY r.pr_url, r.started_at, r.id
)
SELECT (started_at AT TIME ZONE 'UTC')::date AS day, ticket_key AS ticket, project, pr_url,
       coalesce(details->'check_gaps', '[]'::jsonb) AS check_gaps,
       ARRAY(SELECT check_item.item->>'name'
             FROM jsonb_array_elements(details->'ci') AS check_item(item)
             WHERE check_item.item->>'state' = 'failure' ORDER BY check_item.item->>'name') AS failed_jobs
FROM first_checked
WHERE started_at >= ((now() AT TIME ZONE 'UTC')::date - %(back)s::int)::timestamp AT TIME ZONE 'UTC'
ORDER BY day DESC, project, ticket
"""


def first_pipeline_failures(db, days: int, project_keys: list[str] | tuple[str, ...] | None):
    """Load the first observed CI result for pull requests within the caller's project scope."""
    return db.all(FIRST_PIPELINE_FAILURES_SQL, {"back": days - 1, "project_keys_is_null": project_keys is None,
                                                "project_keys": list(project_keys or ())})
