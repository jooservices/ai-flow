"""Persistence for retained run diagnostics and durable ticket coordinator handoffs."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from ...db import J
from .models import FailureRun, RunSnapshot, TicketHandoff


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def run_snapshot(db, run_id: str) -> RunSnapshot | None:
    if not _is_uuid(run_id):
        return None  # runs are stored under UUIDs: any other id has no stored run to snapshot
    row = db.one(
        "SELECT r.id,r.ticket_key,r.step,r.node,r.account_id,r.provider,r.model,r.started_at,r.ended_at,"
        "r.heartbeat_at,r.status,r.outcome,r.error_kind,r.summary,r.progress,r.details,r.tokens_in,r.tokens_out,r.cost_usd,"
        "h.heartbeat_at AS node_heartbeat_at,h.metrics AS node_metrics FROM runs r LEFT JOIN LATERAL ("
        " SELECT heartbeat_at,metrics FROM node_heartbeat_samples WHERE node=r.node"
        " AND heartbeat_at<=coalesce(r.ended_at,now()) ORDER BY heartbeat_at DESC,id DESC LIMIT 1"
        ") h ON true WHERE r.id=%s", (run_id,))
    return _run_snapshot(row) if row else None


def _run_snapshot(row: dict) -> RunSnapshot:
    return RunSnapshot(
        run_id=str(row["id"]), ticket_key=row["ticket_key"], step=row["step"], node=row["node"],
        account_id=row["account_id"], provider=row["provider"], model=row["model"],
        started_at=row["started_at"], ended_at=row["ended_at"], heartbeat_at=row["heartbeat_at"], status=row["status"],
        outcome=row["outcome"] or row["status"], error_kind=row["error_kind"] or "", summary=row["summary"] or "",
        progress=row["progress"] or {}, details=row["details"] or {}, tokens_in=int(row["tokens_in"] or 0),
        tokens_out=int(row["tokens_out"] or 0), cost_usd=row["cost_usd"],
        node_heartbeat_at=row["node_heartbeat_at"], node_metrics=row["node_metrics"] or {})


def save_diagnostic(db, run_id: str, diagnostic: dict[str, Any]) -> None:
    db.run("UPDATE runs SET details=details || %s WHERE id=%s", (J({"diagnostic": diagnostic}), run_id))


def failure_sequence(db, ticket_key: str) -> list[FailureRun]:
    rows = db.all(
        "SELECT id,outcome,started_at,ended_at,details FROM runs WHERE ticket_key=%s"
        " AND step IN ('dev','verify') AND status<>'running' AND ended_at IS NOT NULL"
        " AND ended_at>coalesce((SELECT resolved_at FROM ticket_handoffs WHERE ticket_key=%s"
        " AND state='resolved'),'-infinity'::timestamptz)"
        " ORDER BY ended_at DESC,started_at DESC,id DESC", (ticket_key, ticket_key))
    return [FailureRun(str(row["id"]), row["outcome"] or "", row["started_at"], row["ended_at"],
                       (row["details"] or {}).get("diagnostic") or {}) for row in rows]


def active_handoff(db, ticket_key: str) -> TicketHandoff | None:
    row = db.one("SELECT h.ticket_key,h.incident_id,h.state,i.title,i.detail,h.resolved_at"
                 " FROM ticket_handoffs h JOIN incidents i ON i.id=h.incident_id"
                 " WHERE h.ticket_key=%s AND h.state='blocked' AND i.closed_at IS NULL", (ticket_key,))
    return _handoff(row) if row else None


def _handoff(row: dict) -> TicketHandoff:
    return TicketHandoff(row["ticket_key"], int(row["incident_id"]), row["state"], row["title"],
                         row["detail"] or "", row["resolved_at"])


def blocked_handoff_tickets(db, ticket_keys: list[str] | None = None) -> set[str]:
    if ticket_keys is not None and not ticket_keys:
        return set()
    sql = "SELECT ticket_key FROM ticket_handoffs WHERE state='blocked'"
    params: tuple = ()
    if ticket_keys is not None:
        sql += " AND ticket_key=ANY(%s)"
        params = (ticket_keys,)
    return {row["ticket_key"] for row in db.all(sql, params)}


def upsert_incident(db, ticket_key: str, title: str, detail: str) -> int:
    row = db.one("INSERT INTO incidents (kind,subject,severity,title,detail)"
                 " VALUES ('ticket_stuck',%s,'warning',%s,%s)"
                 " ON CONFLICT (kind,subject) WHERE closed_at IS NULL DO UPDATE SET"
                 " severity=EXCLUDED.severity,title=EXCLUDED.title,detail=EXCLUDED.detail RETURNING id",
                 (ticket_key, title, detail))
    return int(row["id"])


def activate_handoff(db, ticket_key: str, incident_id: int) -> None:
    db.run("INSERT INTO ticket_handoffs (ticket_key,incident_id,state) VALUES (%s,%s,'blocked')"
           " ON CONFLICT (ticket_key) DO UPDATE SET incident_id=EXCLUDED.incident_id,state='blocked',"
           " handoff_at=now(),resolved_at=NULL,resolved_by='',resolution=''", (ticket_key, incident_id))


def handoff_by_incident(db, incident_id: int) -> TicketHandoff | None:
    row = db.one("SELECT h.ticket_key,h.incident_id,h.state,i.title,i.detail,h.resolved_at"
                 " FROM ticket_handoffs h JOIN incidents i ON i.id=h.incident_id"
                 " WHERE h.incident_id=%s AND h.state='blocked' AND i.closed_at IS NULL", (incident_id,))
    return _handoff(row) if row else None


def ticket(db, ticket_key: str) -> dict | None:
    return db.one("SELECT key,project,status FROM tickets WHERE key=%s", (ticket_key,))


def active_runs(db, ticket_key: str) -> bool:
    return bool(db.one("SELECT 1 AS active FROM runs WHERE ticket_key=%s AND status='running' LIMIT 1",
                       (ticket_key,)))


def save_resolution(db, handoff: TicketHandoff, actor: str, resolution: str) -> None:
    db.run("UPDATE ticket_handoffs SET state='resolved',resolved_at=now(),resolved_by=%s,resolution=%s"
           " WHERE ticket_key=%s AND incident_id=%s AND state='blocked'",
           (actor, resolution, handoff.ticket_key, handoff.incident_id))


def set_incident_detail(db, incident_id: int, detail: str) -> None:
    db.run("UPDATE incidents SET detail=%s WHERE id=%s", (detail, incident_id))


def run_exists(db, run_id: str) -> bool:
    return bool(db.one("SELECT 1 AS found FROM runs WHERE id=%s", (run_id,)))


def mark_ticket_todo(db, ticket_key: str) -> None:
    db.run("UPDATE tickets SET status='To Do',synced_at=now() WHERE key=%s", (ticket_key,))


def release_retry_hold(db, ticket_key: str, label: str) -> bool:
    db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, ticket_key))
    return bool(db.one("UPDATE retry_holds SET released_at=now() WHERE ticket_key=%s AND released_at IS NULL"
                       " RETURNING ticket_key", (ticket_key,)))


def audit_resolution(db, ticket_key: str, actor: str, resolution: str) -> None:
    db.run("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
           " VALUES ('jira','coordinator_requeue',%s,%s,true,'To Do',%s)",
           (ticket_key, actor, J({"resolution": resolution})))


def mark_notified(db, incident_id: int, notified: bool) -> None:
    """Record whether an incident reached a notification channel."""
    db.run("UPDATE incidents SET notified=%s WHERE id=%s", (notified, incident_id))


def lock_ticket_handoff(db, ticket_key: str) -> None:
    """Serialize hand-off decisions of one ticket for the rest of the transaction."""
    db.one("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"ticket-handoff:{ticket_key}",))
