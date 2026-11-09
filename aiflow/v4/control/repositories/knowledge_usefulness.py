"""Persistence and report queries for delivered knowledge ids and reported use."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db import J


def record_context_pack(db, *, run_id: str, project: str, ticket_key: str, epic: str,
                        items: list[dict[str, Any]], token_count: int) -> None:
    """Persist one pack and its delivered ids, incrementing each item's handoff count once per run."""
    with db.transaction():
        db.run("INSERT INTO knowledge_context_packs (run_id,project,ticket_key,epic,item_count,token_count) "
               "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO UPDATE SET item_count=EXCLUDED.item_count, "
               "token_count=EXCLUDED.token_count",
               (run_id, project, ticket_key, epic, len(items), token_count))
        for item in items:
            inserted = db.run("INSERT INTO knowledge_deliveries (run_id,item_id,project,ticket_key) "
                              "VALUES (%s,%s,%s,%s) ON CONFLICT (run_id,item_id) DO NOTHING",
                              (run_id, item["id"], project, ticket_key))
            if inserted:
                db.run("UPDATE knowledge_items SET handed_count=handed_count+1,last_handed_at=now() WHERE id=%s",
                       (item["id"],))


def record_run_usefulness(connection, *, run_id: str, ticket_key: str, reported_ids: list[int]) -> list[int]:
    """Store only ids the run received and save its cumulative verification rework count in the result transaction."""
    candidates = sorted({int(item_id) for item_id in reported_ids if int(item_id) > 0})
    delivered = connection.execute("SELECT item_id FROM knowledge_deliveries WHERE run_id=%s "
                                   "AND item_id=ANY(%s) ORDER BY item_id",
                                   (run_id, candidates)).fetchall() if candidates else []
    used_ids = [int(row["item_id"]) for row in delivered]
    for item_id in used_ids:
        connection.execute("INSERT INTO knowledge_uses (run_id,item_id) VALUES (%s,%s) "
                           "ON CONFLICT (run_id,item_id) DO NOTHING", (run_id, item_id))
    rework = connection.execute("SELECT count(*)::int AS count FROM runs WHERE ticket_key=%s "
                                "AND step IN ('verify','qa') AND outcome='rework' AND status='finished'",
                                (ticket_key,)).fetchone() or {"count": 0}
    connection.execute("UPDATE runs SET details=details || %s WHERE id=%s",
                       (J({"knowledge_used_ids": used_ids, "rework_count": int(rework["count"] or 0)}), run_id))
    return used_ids


def _scope_filters(project: str | None, start: datetime | None, end: datetime | None,
                   time_column: str) -> tuple[str, tuple[Any, ...]]:
    filters, params = [], []
    if project is not None:
        if project:
            filters.append("(project=%s OR project='')")
            params.append(project)
        else:
            filters.append("project=''")
    if start is not None:
        filters.append(f"{time_column}>=%s")
        params.append(start)
    if end is not None:
        filters.append(f"{time_column}<%s")
        params.append(end)
    return (" AND ".join(filters) or "true"), tuple(params)


def _report_cte(project: str | None, start: datetime | None, end: datetime | None) -> tuple[str, tuple[Any, ...]]:
    pack_filter, pack_params = _scope_filters(project, start, end, "created_at")
    run_filter, run_params = _scope_filters(project, start, end, "started_at")
    sql = ("WITH scoped_packs AS (SELECT run_id,token_count FROM knowledge_context_packs WHERE " + pack_filter + " "
           "AND NOT EXISTS (SELECT 1 FROM runs release WHERE release.id=knowledge_context_packs.run_id "
           "AND release.step='version_release')), "
           "scoped_deliveries AS (SELECT d.run_id,d.item_id FROM knowledge_deliveries d "
           "JOIN scoped_packs p ON p.run_id=d.run_id), "
           "scoped_uses AS (SELECT u.run_id,u.item_id FROM knowledge_uses u "
           "JOIN scoped_deliveries d ON d.run_id=u.run_id AND d.item_id=u.item_id), "
           "scoped_runs AS (SELECT r.id,r.ticket_key,r.started_at,r.outcome,r.details, "
           "(r.details->>'verdict'='PASS' AND r.details->>'qc_passed'='true' AND r.outcome<>'rework' AND "
           "(r.details->'rework_count'='0'::jsonb OR (r.details->>'rework_count' IS NULL AND NOT EXISTS ("
           "SELECT 1 FROM runs prior WHERE prior.ticket_key=r.ticket_key AND prior.step IN ('verify','qa') "
           "AND prior.status='finished' AND prior.outcome='rework' "
           "AND (prior.started_at,prior.id)<(r.started_at,r.id))))) AS first_pass "
           "FROM runs r WHERE " + run_filter + " AND r.step='verify' AND r.status='finished') ")
    return sql, pack_params + run_params


def _grouped_report_rows(db, project: str | None, start: datetime | None, end: datetime | None) -> list[dict]:
    cte, params = _report_cte(project, start, end)
    visibility = "true"
    if project is not None:
        visibility = "(k.scope='general' OR (k.scope='project' AND k.project=%s))" if project else "k.scope='general'"
    if project:
        params += (project,)
    query = (cte + "SELECT GROUPING(k.id)::int AS by_kind,k.id,k.kind,k.title,k.status,k.scope,k.project, "
             "count(d.item_id)::int AS delivered_count,count(u.item_id)::int AS used_count, "
             "count(DISTINCT r.id)::int AS verify_runs, "
             "count(DISTINCT r.id) FILTER (WHERE r.outcome='rework')::int AS rework_runs, "
             "count(DISTINCT r.id) FILTER (WHERE r.first_pass)::int AS first_pass_runs "
             "FROM knowledge_items k LEFT JOIN scoped_deliveries d ON d.item_id=k.id "
             "LEFT JOIN scoped_uses u ON u.run_id=d.run_id AND u.item_id=d.item_id "
             "LEFT JOIN scoped_runs r ON r.id=d.run_id WHERE " + visibility + " "
             "GROUP BY GROUPING SETS ((k.id,k.kind,k.title,k.status,k.scope,k.project),(k.kind)) "
             "ORDER BY GROUPING(k.id),k.kind,k.title NULLS LAST")
    return db.all(query, params)


def _report_totals(db, project: str | None, start: datetime | None, end: datetime | None) -> dict:
    cte, params = _report_cte(project, start, end)
    row = db.one(cte + "SELECT count(*)::int AS verify_runs, "
                 "count(*) FILTER (WHERE outcome='rework')::int AS rework_runs, "
                 "count(*) FILTER (WHERE first_pass)::int "
                 "AS first_pass_runs FROM scoped_runs", params) or {}
    packs = db.one(cte + "SELECT count(*)::int AS count,count(token_count)::int AS known_token_counts, "
                   "coalesce(sum(token_count),0)::int AS tokens FROM scoped_packs", params) or {}
    return {**row, **packs}


def _cohort(runs: int, rework_runs: int, first_pass_runs: int) -> dict[str, Any]:
    return {"verify_runs": runs, "rework_runs": rework_runs,
            "rework_rate": round(rework_runs / runs, 4) if runs else None,
            "first_pass_runs": first_pass_runs,
            "first_pass_rate": round(first_pass_runs / runs, 4) if runs else None}


def _comparison(totals: dict, group: dict) -> dict[str, dict[str, Any]]:
    with_item = _cohort(group["verify_runs"], group["rework_runs"], group["first_pass_runs"])
    without = _cohort(max(0, totals["verify_runs"] - with_item["verify_runs"]),
                      max(0, totals["rework_runs"] - with_item["rework_runs"]),
                      max(0, totals["first_pass_runs"] - with_item["first_pass_runs"]))
    return {"with_item": with_item, "without_item": without}


def report(db, *, project: str | None = None, start: datetime | None = None,
           end: datetime | None = None) -> dict[str, Any]:
    """Return item and kind counts, verification comparisons and items eligible for retirement review."""
    totals = _report_totals(db, project, start, end)
    item_rows, kind_rows = [], []
    for row in _grouped_report_rows(db, project, start, end):
        group = {**row, "comparison": _comparison(totals, row)}
        if row["by_kind"]:
            kind_rows.append({"kind": row["kind"], "delivered_count": row["delivered_count"],
                              "used_count": row["used_count"], "comparison": group["comparison"]})
        else:
            item_rows.append({key: group[key] for key in
                              ("id", "kind", "title", "status", "scope", "project", "delivered_count",
                               "used_count", "comparison")})
    retirement = [row for row in item_rows if row["status"] != "retired" and row["delivered_count"] >= 10
                  and row["used_count"] == 0]
    return {"items": item_rows, "by_kind": kind_rows, "retirement_review": retirement,
            "verification_runs": int(totals.get("verify_runs") or 0),
            "pack_tokens": {"packs": int(totals.get("count") or 0),
                            "known_packs": int(totals.get("known_token_counts") or 0),
                            "tokens": int(totals.get("tokens") or 0)}}
