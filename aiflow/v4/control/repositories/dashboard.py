"""Database queries for Dashboard read models."""
from __future__ import annotations


def flow_failure_runs(db, scope: list[str] | None) -> list[dict]:
    """Read terminal failures in the last day, limited to visible projects when scoped."""
    project_filter = " AND r.project=ANY(%s)" if scope is not None else ""
    params = (scope,) if scope is not None else ()
    return db.all(
        "SELECT r.id,r.project,r.ticket_key,r.step,r.outcome,r.error_kind,r.summary,r.tokens_in,r.tokens_out,"
        "r.cost_usd,r.started_at FROM runs r WHERE coalesce(r.ended_at,r.started_at) >= now()-interval '24 hours'"
        " AND r.status<>'running' AND r.outcome IN ('failed','timeout')" + project_filter + " ORDER BY r.started_at",
        params,
    )


def quota_sources(db) -> tuple[list[dict], list[dict]]:
    """Read active node project scopes and account quota data for the Dashboard."""
    nodes = db.all("SELECT name,projects,dynamic FROM nodes WHERE removed_at IS NULL")
    accounts = db.all("SELECT id,provider,CASE WHEN quota_source<>'' THEN quota ELSE '{}'::jsonb END AS quota,"
                      "quota_source,quota_observed_at,quota_forecast FROM accounts ORDER BY node,provider,id")
    return nodes, accounts


def total_runs(db, scope: list[str] | None) -> int:
    """Count non-running rows from the last day within the caller's project scope."""
    project_filter = " AND project=ANY(%s)" if scope is not None else ""
    params = (scope,) if scope is not None else ()
    return int(db.one("SELECT count(*) AS n FROM runs WHERE coalesce(ended_at,started_at) >= now()-interval '24 hours'"
                      " AND status<>'running'" + project_filter, params)["n"] or 0)


def flow_failure_trackers(db, signatures: list[str]) -> list[dict]:
    """Read the tracked bug state for signatures shown in the Dashboard."""
    if not signatures:
        return []
    return db.all(
        "SELECT f.signature,f.ticket_key,f.regression_count,coalesce(t.status,f.bug_status,'') AS state"
        " FROM flow_bug_tracker f LEFT JOIN tickets t ON t.key=f.ticket_key WHERE f.signature=ANY(%s)",
        (signatures,),
    )
