"""Daily analytics: runs, outcomes, tokens, cost and median run time per UTC day, project and account.

Read straight from the runs table (no rollup table): a day is the UTC date a run started. "failed" counts every
run that ended without work (failed, blocked, timeout, lost), the same set as the Command Center 24 h totals.
"""
from __future__ import annotations

from typing import Any

FAILED = ("failed", "blocked", "timeout", "lost")
DAILY_SQL = """
SELECT (r.started_at AT TIME ZONE 'UTC')::date AS day, r.project, r.account_id AS account,
       count(*) AS runs,
       count(*) FILTER (WHERE r.outcome = 'done') AS done,
       count(*) FILTER (WHERE r.outcome = ANY(%(failed)s::text[])) AS failed,
       count(*) FILTER (WHERE r.outcome = 'rework') AS rework,
       count(*) FILTER (WHERE r.outcome = 'merged') AS merged,
       coalesce(sum(r.tokens_in), 0) AS tokens_in, coalesce(sum(r.tokens_out), 0) AS tokens_out,
       coalesce(sum(r.cost_usd), 0) AS cost_usd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM r.ended_at - r.started_at)::float8)
         FILTER (WHERE r.ended_at IS NOT NULL) AS median_run_s
FROM runs r
WHERE r.started_at >= ((now() AT TIME ZONE 'UTC')::date - %(back)s::int)::timestamp AT TIME ZONE 'UTC'
  AND (%(project)s::text = '' OR r.project = %(project)s::text)
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 2, 3
"""


def daily(db, days: int = 14, project: str = "") -> list[dict[str, Any]]:
    """One row per (day, project, account) that had runs in the last `days` UTC days, today included."""
    rows = db.all(DAILY_SQL, {"failed": list(FAILED), "back": days - 1, "project": project})
    return [_row(r) for r in rows]


def _row(r: dict) -> dict[str, Any]:
    med = r["median_run_s"]
    return {"day": r["day"].isoformat(), "project": r["project"], "account": r["account"],
            "runs": r["runs"], "done": r["done"], "failed": r["failed"], "rework": r["rework"], "merged": r["merged"],
            "tokens_in": int(r["tokens_in"]), "tokens_out": int(r["tokens_out"]),
            "cost_usd": round(float(r["cost_usd"]), 6), "median_run_s": None if med is None else round(float(med), 1)}
