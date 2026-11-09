"""SQL persistence mapping for ticket assumptions and contradiction tracking."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def record_assumptions(
    db: Any,
    ticket_key: str,
    project: str,
    run_id: str,
    step: str,
    assumptions: list[dict[str, str]],
    source: str = "flow",
) -> list[dict[str, Any]]:
    """Insert recorded assumptions for a ticket into the durable repository."""
    recorded = []
    for item in assumptions:
        assumption = item.get("assumption", "").strip()
        why = item.get("why", "").strip()
        if not assumption:
            continue
        row = db.one(
            "INSERT INTO assumptions (ticket_key, project, run_id, step, assumption, why, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (ticket_key, md5(assumption)) DO NOTHING "
            "RETURNING id, ticket_key, project, run_id, step, assumption, why, source, created_at, contradicted",
            (ticket_key, project, run_id or "", step or "intake", assumption, why, source or "flow"),
        )
        if row:
            recorded.append(row)
    return recorded


def contradict_assumptions(
    db: Any,
    ticket_key: str,
    run_id: str,
    reason: str,
    assumptions: list[str],
) -> int:
    """Mark the named, still uncontradicted assumptions of a ticket as contradicted by a verification."""
    if not assumptions:
        return 0
    rows = db.all(
        "UPDATE assumptions "
        "SET contradicted = true, contradicted_at = now(), contradicted_by_run_id = %s, contradiction_reason = %s "
        "WHERE ticket_key = %s AND NOT contradicted AND lower(btrim(assumption)) = ANY(%s) "
        "RETURNING id",
        (run_id or "", reason[:1000] if reason else "contradicted by verification", ticket_key,
         [text.strip().lower() for text in assumptions]),
    )
    return len(rows)


def assumptions_daily(db: Any, days: int = 14, projects: list[str] | None = None) -> list[dict[str, Any]]:
    """Return assumptions made per UTC day (whole days only) and how many of them were later contradicted."""
    first_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=max(1, days) - 1)
    rows = db.all(
        "SELECT date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS day, "
        "count(*)::int AS total, "
        "count(*) FILTER (WHERE contradicted)::int AS contradicted "
        "FROM assumptions "
        "WHERE created_at >= %s AND (%s::text[] IS NULL OR project = ANY(%s)) "
        "GROUP BY 1 "
        "ORDER BY 1 DESC",
        (first_day, projects, projects),
    )
    return [
        {"day": str(row["day"]), "total": int(row["total"]), "contradicted": int(row["contradicted"])}
        for row in rows
    ]


def _assumption_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticket_key": row["ticket_key"],
        "project": row["project"],
        "run_id": row["run_id"],
        "step": row["step"],
        "assumption": row["assumption"],
        "why": row["why"],
        "source": row["source"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else "",
        "contradicted": bool(row.get("contradicted")),
        "contradicted_at": row["contradicted_at"].isoformat() if row.get("contradicted_at") else None,
        "contradiction_reason": row.get("contradiction_reason") or "",
    }


def recent_assumptions(db: Any, limit: int = 20, projects: list[str] | None = None) -> list[dict[str, Any]]:
    """Return the most recently recorded assumptions with their contradiction state."""
    rows = db.all(
        "SELECT id, ticket_key, project, run_id, step, assumption, why, source, created_at, "
        "contradicted, contradicted_at, contradiction_reason "
        "FROM assumptions "
        "WHERE (%s::text[] IS NULL OR project = ANY(%s)) "
        "ORDER BY created_at DESC "
        "LIMIT %s",
        (projects, projects, limit),
    )
    return [_assumption_view(row) for row in rows]


def assumptions_for_ticket(db: Any, ticket_key: str) -> list[dict[str, Any]]:
    """Return all recorded assumptions for one ticket."""
    rows = db.all(
        "SELECT id, ticket_key, project, run_id, step, assumption, why, source, created_at, "
        "contradicted, contradicted_at, contradiction_reason "
        "FROM assumptions "
        "WHERE ticket_key = %s "
        "ORDER BY created_at ASC",
        (ticket_key,),
    )
    return [_assumption_view(row) for row in rows]
