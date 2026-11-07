"""Persistence for the Dashboard's master review request and the master cycle metrics (JAIF-349)."""
from __future__ import annotations

from datetime import datetime


def request_review(db) -> None:
    """Mark that the owner asked for a master review; the request waits for the next snapshot."""
    db.run("UPDATE master_settings SET review_requested_at=now() WHERE singleton=true")


def take_review_request(db) -> bool:
    """Clear a waiting request and report whether one was waiting."""
    taken = db.one("UPDATE master_settings SET review_requested_at=NULL WHERE singleton=true"
                   " AND review_requested_at IS NOT NULL RETURNING true AS taken")
    return bool(taken)


def cycle_counts_by_day(db, since: datetime) -> list[dict]:
    """Master cycles, AI calls and cycles that completed at least one action, per UTC day since a start time."""
    return db.all("SELECT (r.started_at AT TIME ZONE 'UTC')::date AS day, count(*) AS cycles,"
                  " count(*) FILTER (WHERE r.details->>'no_watch' IS DISTINCT FROM 'true') AS ai_calls,"
                  " count(*) FILTER (WHERE coalesce((r.details->>'completed')::int, 0) > 0) AS actions"
                  " FROM runs r WHERE r.step='master' AND r.started_at >= %s GROUP BY 1 ORDER BY 1", (since,))
