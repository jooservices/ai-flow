"""Run diagnostics, repeated-failure handoffs, and coordinator resolution use cases."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ... import incidents, merge_flow, notify, retry_hold, support_bundle
from ...platform.errors import Conflict, NotFound, Unavailable
from ..work.events import RunFinished
from . import repository
from ...platform.events.wiring import publish_for
from .events import IncidentOpened
from .models import FailureRun, RunSnapshot

UNAVAILABLE = "unavailable"
DETAIL_PREFIX = "handoff-v1:"
UNSUCCESSFUL = {"failed", "timeout", "lost", "no_changes"}


def safe_text(value: Any, limit: int = 500) -> str:
    raw = " ".join(str(value or "").split())[:limit]
    if not raw:
        return UNAVAILABLE
    cleaned = support_bundle.sanitize_text(raw, names=False)
    return cleaned[:limit] if not support_bundle.sensitive_findings(cleaned) else "unavailable (sensitive text omitted)"


def _time(value: datetime | None) -> str:
    return value.isoformat() if value else UNAVAILABLE


def _reported(value: Any, reported: bool) -> int | float | str:
    if not reported or value is None:
        return UNAVAILABLE
    try:
        number = float(value)
    except (TypeError, ValueError):
        return UNAVAILABLE
    return int(number) if isinstance(value, int) else number


def _node_heartbeat(run: RunSnapshot) -> dict[str, Any]:
    metrics = run.node_metrics
    cpu_pct, load1, cpus = metrics.get("cpu_pct"), metrics.get("load1"), metrics.get("cpus")
    memory_used, memory_total = metrics.get("mem_used"), metrics.get("mem_total")
    cpu = _ratio(load1, cpus)
    memory = _percent(memory_used, memory_total)
    return {"heartbeat_at": _time(run.node_heartbeat_at), "cpu_pct": _number(cpu_pct),
            "cpu_load_per_cpu": cpu,
            "load1": _number(load1), "cpus": _number(cpus), "memory_used_pct": memory,
            "memory_used_bytes": _number(memory_used), "memory_total_bytes": _number(memory_total)}


def _number(value: Any) -> float | str:
    return float(value) if isinstance(value, (int, float)) else UNAVAILABLE


def _ratio(value: Any, divisor: Any) -> float | str:
    if not isinstance(value, (int, float)) or not isinstance(divisor, (int, float)) or divisor <= 0:
        return UNAVAILABLE
    return round(value / divisor, 4)


def _percent(value: Any, total: Any) -> float | str:
    ratio = _ratio(value, total)
    return round(ratio * 100, 2) if isinstance(ratio, float) else UNAVAILABLE


def _progress_value(run: RunSnapshot, key: str) -> str:
    value = run.progress.get(key)
    return safe_text(value, 160) if value not in (None, "") else UNAVAILABLE


def _test_progress(run: RunSnapshot) -> dict[str, str] | str:
    details = run.details
    fields = ("test_progress", "test_command", "test_suite", "tests", "checks")
    return {key: safe_text(details[key], 500) for key in fields if details.get(key) not in (None, "", [])} or UNAVAILABLE


def diagnostic_record(run: RunSnapshot, reason: str = "") -> dict[str, Any]:
    """Build the allow-listed record; free-form values pass through the support secret scanner."""
    reported = run.details.get("diagnostic_usage_reported") or {}
    tokens_in = _reported(run.tokens_in, bool(reported.get("tokens_in")))
    tokens_out = _reported(run.tokens_out, bool(reported.get("tokens_out")))
    cost = _reported(run.cost_usd, bool(reported.get("cost_usd")))
    duration = max(0, int((run.ended_at - run.started_at).total_seconds() * 1000)) if run.ended_at else UNAVAILABLE
    return {"ticket": run.ticket_key, "run_id": run.run_id, "step": safe_text(run.step, 40),
            "node": safe_text(run.node, 120), "account": safe_text(run.account_id, 120),
            "provider": safe_text(run.provider, 80), "model": safe_text(run.model, 160),
            "started_at": _time(run.started_at), "ended_at": _time(run.ended_at), "duration_ms": duration,
            "outcome": safe_text(run.outcome, 80), "error_kind": safe_text(run.error_kind, 120),
            "summary": safe_text(run.summary, 800), "reason": safe_text(reason, 300),
            "progress_phase": _progress_value(run, "phase"),
            "last_progress_event": _progress_value(run, "last"),
            "progress_reported_at": _progress_value(run, "reported_at"), "test_progress": _test_progress(run),
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "tokens": tokens_in + tokens_out if isinstance(tokens_in, (int, float)) and isinstance(tokens_out, (int, float)) else UNAVAILABLE,
            "cost_usd": cost, "node_heartbeat": _node_heartbeat(run),
            "run_log_url": f"/v1/runs/{run.run_id}/diagnostic-log"}


def consecutive_failures(runs: list[FailureRun]) -> list[FailureRun]:
    failed = []
    for run in runs:
        if run.outcome not in UNSUCCESSFUL:
            break
        failed.append(run)
    return failed


def _diagnostic_for(ctx, failure: FailureRun) -> dict[str, Any]:
    if failure.diagnostic:
        return failure.diagnostic
    run = repository.run_snapshot(ctx.db, failure.run_id)
    if run is None:
        return {"ticket": "unavailable", "run_id": failure.run_id, "diagnostic_unavailable": True}
    diagnostic = diagnostic_record(run)
    repository.save_diagnostic(ctx.db, run.run_id, diagnostic)
    return diagnostic


def _unknowns(records: list[dict[str, Any]]) -> list[str]:
    unknowns = []
    for record in records:
        for key in ("progress_phase", "last_progress_event", "test_progress", "tokens", "cost_usd"):
            if record.get(key) == UNAVAILABLE:
                unknowns.append(f"{record['run_id']}: {key} was unavailable")
        for key, value in (record.get("node_heartbeat") or {}).items():
            if value == UNAVAILABLE:
                unknowns.append(f"{record['run_id']}: node {key} was unavailable")
    return unknowns


def _evidence(records: list[dict[str, Any]]) -> list[str]:
    evidence = []
    for row in records:
        cause = row["summary"] if row["summary"] != UNAVAILABLE else row["reason"]
        evidence.append(f"{row['step']} run {row['run_id']} on {row['node']} ended {row['outcome']}"
                        + (f" ({row['error_kind']})" if row["error_kind"] != UNAVAILABLE else "")
                        + (f": {cause}" if cause != UNAVAILABLE else ""))
    return evidence


def _handoff_payload(failures: list[FailureRun], records: list[dict[str, Any]],
                     notification_channels: list[str]) -> dict[str, Any]:
    first = records[0]
    last = records[-1]
    started = failures[-1].started_at
    ended = failures[0].ended_at
    elapsed = max(0, int((ended - started).total_seconds() * 1000))
    gap = ("No enabled coordinator/operator destination is routed for Ticket needs a person. "
           "Configure an alert channel on the Notifications screen." if not notification_channels else "")
    return {"schema": "aiflow.ticket-handoff/v1", "handoff_state": "blocked",
            "streak_count": len(failures), "elapsed_ms": elapsed,
            "started_at": first["started_at"], "ended_at": last["ended_at"], "runs": records,
            "cause": {"evidence": _evidence(records), "unknowns": _unknowns(records)},
            "notification": {"state": "pending" if notification_channels else "missing_destination",
                             "channels": notification_channels, "configuration_gap": gap}}


def _encode_payload(payload: dict[str, Any]) -> str:
    return DETAIL_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def handoff_payload(detail: str) -> dict[str, Any] | None:
    if not detail.startswith(DETAIL_PREFIX):
        return None
    try:
        payload = json.loads(detail[len(DETAIL_PREFIX):])
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema") == "aiflow.ticket-handoff/v1" else None


def notification_detail(payload: dict[str, Any]) -> str:
    lines = [f"{payload['streak_count']} consecutive unsuccessful developer/verification runs; "
             f"elapsed {round(payload['elapsed_ms'] / 60000, 1)} minutes."]
    lines.extend(payload.get("cause", {}).get("evidence", [])[-3:])
    lines.append("Run logs: " + ", ".join(row["run_log_url"] for row in payload.get("runs", [])))
    lines.extend(payload.get("cause", {}).get("unknowns", [])[:8])
    gap = payload.get("notification", {}).get("configuration_gap")
    if gap:
        lines.append(gap)
    return "\n".join(lines)[:2800]


def set_notification_state(ctx, incident_id: int, detail: str, state: str, channels: list[str]) -> None:
    payload = handoff_payload(detail)
    if not payload:
        return
    configuration_gap = ("No enabled coordinator/operator destination is routed for Ticket needs a person. "
                         "Configure an alert channel on the Notifications screen." if state == "missing_destination" else "")
    payload["notification"] = {"state": state, "channels": channels, "configuration_gap": configuration_gap}
    repository.set_incident_detail(ctx.db, incident_id, _encode_payload(payload))


def record_run_outcome(ctx, item: RunFinished) -> None:
    """Persist one developer/verification diagnostic and open the handoff in the outcome transaction."""
    with ctx.db.transaction():
        run = repository.run_snapshot(ctx.db, item.run_id)
        if run is None or run.step not in ("dev", "verify") or run.status == "running":
            return
        repository.lock_ticket_handoff(ctx.db, run.ticket_key)
        current = diagnostic_record(run, item.reason)
        repository.save_diagnostic(ctx.db, item.run_id, current)
        failures = consecutive_failures(repository.failure_sequence(ctx.db, run.ticket_key))
        if len(failures) < 3:
            return
        _upsert_handoff(ctx, run, failures)


def _upsert_handoff(ctx, run: RunSnapshot, failures: list[FailureRun]) -> None:
    records = [_diagnostic_for(ctx, failure) for failure in reversed(failures[:3])]
    previous = repository.active_handoff(ctx.db, run.ticket_key)
    previous_payload = handoff_payload(previous.detail) if previous else None
    current_ids = [record.get("run_id") for record in records]
    if previous_payload and [record.get("run_id") for record in previous_payload.get("runs", [])] == current_ids:
        return
    channels = notify.configured_channels(ctx, "ticket_needs_person")
    payload = _handoff_payload(failures, records, channels)
    if previous_payload:
        payload["notification"] = previous_payload.get("notification", payload["notification"])
    detail = _encode_payload(payload)
    title = f"{run.ticket_key} failed {len(failures)} runs in a row"
    incident_id = repository.upsert_incident(ctx.db, run.ticket_key, title, detail)
    if previous is None:
        repository.activate_handoff(ctx.db, run.ticket_key, incident_id)
        publish_for(ctx, IncidentOpened(incident_id=incident_id, kind="ticket_stuck", subject=run.ticket_key,
                                        severity="warning", title=title, detail=detail))


def update_notification(ctx, incident_id: int, detail: str, channels: list[str], configured: list[str]) -> None:
    if not configured:
        set_notification_state(ctx, incident_id, detail, "missing_destination", [])
    elif channels:
        set_notification_state(ctx, incident_id, detail, "queued", channels)
    else:
        set_notification_state(ctx, incident_id, detail, "delivery_unconfirmed", configured)


def run_log_text(ctx, run_id: str) -> str | None:
    if not repository.run_exists(ctx.db, run_id):
        return None
    return support_bundle.load_run_log(ctx, run_id)


def blocked_handoff_tickets(ctx) -> set[str]:
    return repository.blocked_handoff_tickets(ctx.db)


def _resolved_detail(detail: str, actor: str, resolution: str) -> str:
    payload = handoff_payload(detail) or {}
    payload["handoff_state"] = "resolved"
    payload["resolution"] = {"actor": safe_text(actor, 120), "text": safe_text(resolution, 1000)}
    return _encode_payload(payload)


def resolve_and_requeue(ctx, incident_id: int, actor: str, resolution: str) -> dict[str, Any]:
    """Only the coordinator action closes the durable hold, after Jira confirms the requeue."""
    clean_resolution = safe_text(resolution, 1000)
    if clean_resolution.startswith(UNAVAILABLE) or len(clean_resolution) < 10:
        raise Conflict("enter a resolution of at least 10 safe characters")
    handoff = repository.handoff_by_incident(ctx.db, incident_id)
    if handoff is None:
        raise NotFound("open coordinator handoff was not found")
    if repository.active_runs(ctx.db, handoff.ticket_key):
        raise Conflict("wait for the active run to finish before resolving this handoff")
    if not ctx.jira:
        raise Unavailable("Jira is unavailable; the handoff remains blocked")
    try:
        ctx.jira.comment(handoff.ticket_key, f"AI Flow · coordinator resolution\n{clean_resolution}\nRequeued to To Do.")
        ctx.jira.remove_label(handoff.ticket_key, retry_hold.HOLD_LABEL)
        ctx.jira.transition_to(handoff.ticket_key, "To Do")
    except Exception as exc:  # noqa: BLE001 - keep the handoff active when Jira did not confirm requeue
        raise Unavailable("Jira could not requeue the ticket; the handoff remains blocked") from exc
    with ctx.db.transaction():
        repository.save_resolution(ctx.db, handoff, safe_text(actor, 120), clean_resolution)
        released = repository.release_retry_hold(ctx.db, handoff.ticket_key, retry_hold.HOLD_LABEL)
        if released:
            ctx.events.emit("ticket.hold_released", ticket=handoff.ticket_key,
                            reason="coordinator resolved repeated failures")
        repository.mark_ticket_todo(ctx.db, handoff.ticket_key)
        repository.set_incident_detail(ctx.db, incident_id, _resolved_detail(handoff.detail, actor, clean_resolution))
        repository.audit_resolution(ctx.db, handoff.ticket_key, safe_text(actor, 120), clean_resolution)
        merge_flow.record_move(ctx, handoff.ticket_key, "Coordinator handoff", "To Do", merge_flow.FLOW,
                               "coordinator resolved the repeated failure and requeued the ticket")
        incidents.close_incident(ctx, "ticket_stuck", handoff.ticket_key,
                                  f"Coordinator resolution: {clean_resolution}")
    return {"resolved": True, "ticket": handoff.ticket_key, "status": "To Do"}
