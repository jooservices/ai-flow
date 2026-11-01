"""Named steps for the fenced master action route."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from ....core import work_limits
from ... import notify, run_inbox as run_inbox_control
from ...db import J
from ...incidents import flow_bug_candidates
from ...policy import decide_master_action
from ...features.work.constants import utcnow
from .bodies import MasterActionBody
from .events import MasterEscalationNotified, MasterEscalationRequested
from ...platform.events.wiring import publish_for


@dataclass(frozen=True)
class MasterActionState:
    run: dict[str, Any]
    node: str
    policy_project: str | None
    ticket: str
    body: MasterActionBody


@dataclass(frozen=True)
class EscalationRecord:
    created_row: Any
    reason: str
    options: tuple[str, ...]
    intent: str | None
    configured_channels: tuple[str, ...]
    pending_channels: tuple[str, ...]
    delivered_channels: tuple[str, ...]


@dataclass(frozen=True)
class EscalationDelivery:
    reason: str
    options: tuple[str, ...]
    sent: tuple[str, ...]
    pending_channels: tuple[str, ...]
    delivered_channels: tuple[str, ...]
    already_sent: bool


DEFAULT_ESCALATION_OPTIONS = [
    "Keep the current state and wait for the next health check.",
    "Choose another healthy account or retry after the reported reset time.",
    "Ask an owner to investigate and decide the next step.",
]


def require_intent(ctx, audit, state: MasterActionState) -> None:
    """Record the legacy empty-intent denial before returning HTTP 400."""
    if state.body.intent.strip():
        return
    reason = "master action must include the request or observation intent"
    audit("master", state.body.action, state.ticket, state.body.run_id, state.body.fence,
          state.run["account_id"], False, reason)
    ctx.events.emit("master.action", ticket=state.ticket or state.run["ticket_key"], run_id=state.body.run_id,
                    fence=state.body.fence, account=state.run["account_id"], node=state.node,
                    outcome="denied", reason=reason, action=state.body.action)
    raise HTTPException(400, reason)


def decide(ctx, state: MasterActionState, policy):
    """Apply policy to the request text and emit its decision telemetry."""
    # Observation fields describe context and must not turn an allowed follow-up into an owner escalation.
    decision = decide_master_action(policy, state.body.action, state.body.intent, state.body.category)
    ctx.events.emit("policy.decision", ticket=state.ticket or state.run["ticket_key"], step="master",
                    run_id=state.body.run_id, fence=state.body.fence, account=state.run["account_id"],
                    node=state.node, outcome=decision.decision, reason=decision.reason[:200],
                    action=state.body.action)
    return decision


def master_action_event(ctx, state: MasterActionState, outcome: str, reason: str) -> None:
    ctx.events.emit("master.action", ticket=state.ticket or state.run["ticket_key"],
                    project=state.policy_project or "", step="master", run_id=state.body.run_id,
                    fence=state.body.fence, account=state.run["account_id"], provider=state.run["provider"],
                    model=state.run["model"], node=state.node, outcome=outcome, reason=reason[:300],
                    action=state.body.action)


def refuse(ctx, audit, state: MasterActionState, reason: str, status: int = 409) -> None:
    audit("master", state.body.action, state.ticket, state.body.run_id, state.body.fence,
          state.run["account_id"], False, reason,
          {"intent": state.body.intent[:1000], "why": state.body.why[:1000]})
    master_action_event(ctx, state, "denied", reason)
    raise HTTPException(status, reason)


def deny_policy_decision(ctx, audit, state: MasterActionState, decision) -> None:
    audit("master", state.body.action, state.ticket, state.body.run_id, state.body.fence,
          state.run["account_id"], False, decision.reason, {"intent_chars": len(state.body.intent)})
    master_action_event(ctx, state, "denied", decision.reason)
    raise HTTPException(403, decision.reason)


def escalation_options(options: list[str], *, defaults: bool = True) -> list[str]:
    normalized = [str(option).strip()[:500] for option in options if str(option).strip()][:5]
    return normalized or (list(DEFAULT_ESCALATION_OPTIONS) if defaults else [])


def observation_key_for(body: MasterActionBody, ticket: str, ticket_updated=None) -> str:
    observation_key = body.observation_key.strip()[:500]
    if observation_key:
        return observation_key
    if body.kind and body.subject:
        return f"incident:{body.kind}:{body.subject}"
    if ticket:
        updated_at = ticket_updated["jira_updated"] if ticket_updated else None
        return f"ticket:{ticket}:{updated_at.isoformat() if updated_at else 'current'}"
    return f"master:{body.action}:{body.intent[:200]}"


def record_escalation(ctx, state: MasterActionState, reason: str, options: list[str], observation_key: str):
    configured_channels = notify.configured_channels(ctx, "ticket_needs_person")
    created_row = ctx.db.one(
        "INSERT INTO master_escalations"
        " (observation_key,ticket,reason,options,run_id,pending_channels,delivered_channels,intent)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (observation_key) DO NOTHING RETURNING observation_key",
        (observation_key, state.ticket, reason, J(options), state.body.run_id, configured_channels, [],
         state.body.intent[:1500]))
    escalation = ctx.db.one(
        "SELECT reason, options, pending_channels, delivered_channels, intent FROM master_escalations"
        " WHERE observation_key=%s", (observation_key,))
    # A NULL pending list marks rows created before channel delivery tracking was installed.
    pending_channels = set(escalation.get("pending_channels") or [])
    delivered_channels = set(escalation.get("delivered_channels") or [])
    if escalation.get("pending_channels") is None:
        pending_channels.update(set(configured_channels) - delivered_channels)
    elif not pending_channels and not delivered_channels:
        # An observation first seen without channels can be delivered after channels are configured later.
        pending_channels.update(configured_channels)
    else:
        pending_channels.update(set(configured_channels) - delivered_channels)
    pending_before = sorted(pending_channels)
    if pending_before != sorted(escalation.get("pending_channels") or []):
        ctx.db.run("UPDATE master_escalations SET pending_channels=%s WHERE observation_key=%s",
                   (pending_before, observation_key))
    return EscalationRecord(
        created_row=created_row, reason=str(escalation["reason"]),
        options=tuple(str(option) for option in (escalation.get("options") or [])),
        intent=escalation.get("intent"), configured_channels=tuple(configured_channels),
        pending_channels=tuple(sorted(pending_channels)), delivered_channels=tuple(sorted(delivered_channels)))


def deliver_escalation(ctx, state: MasterActionState, record: EscalationRecord,
                       policy_project: str | None, log) -> EscalationDelivery:
    pending_channels = set(record.pending_channels)
    delivered_channels = set(record.delivered_channels)
    link = (ctx.settings.jira_site.rstrip("/") + f"/browse/{state.ticket}") \
        if state.ticket and ctx.settings.jira_site else ""
    message = (f"Why a person is needed: {record.reason}\nRequest: {record.intent or state.body.intent[:1500]}\n"
               "Available options:\n" + "\n".join(f"- {option}" for option in record.options))
    attempt_channels = sorted(pending_channels.intersection(record.configured_channels))
    try:
        sent = (notify.send("warning", f"{state.ticket or 'AI Flow'} requires an owner decision", message, link,
                            ctx=ctx, alert_kind="ticket_needs_person", project=policy_project or "AI Flow",
                            ticket=state.ticket, needed=record.reason, channels=attempt_channels, queued_ok=True)
                if attempt_channels else [])
    except Exception as exc:  # Persist pending channels and retry them on a later master cycle.
        log.warning("master escalation notification failed: %s", type(exc).__name__)
        sent = []
    sent = sorted(set(sent).intersection(attempt_channels))
    pending_channels.difference_update(sent)
    delivered_channels.update(sent)
    already_sent = bool(not record.created_row and delivered_channels and not pending_channels and not sent)
    return EscalationDelivery(record.reason, record.options, tuple(sent), tuple(sorted(pending_channels)),
                              tuple(sorted(delivered_channels)), already_sent)


def publish_escalation(ctx, audit, state: MasterActionState, decision, observation_key: str,
                       options: list[str], delivery: EscalationDelivery) -> None:
    with ctx.db.transaction():
        ctx.db.run("UPDATE master_escalations SET pending_channels=%s, delivered_channels=%s"
                   " WHERE observation_key=%s",
                   (list(delivery.pending_channels), list(delivery.delivered_channels), observation_key))
        if state.body.action == "escalate_to_owner":
            publish_for(ctx, MasterEscalationRequested(
                run_id=state.body.run_id, fence=state.body.fence, ticket=state.ticket,
                actor=state.run["account_id"], action=state.body.action, reason=decision.reason,
                intent_chars=len(state.body.intent), observation_key=observation_key, options=options))
        else:
            audit("master", state.body.action, state.ticket, state.body.run_id, state.body.fence,
                  state.run["account_id"], False, decision.reason,
                  {"intent_chars": len(state.body.intent), "observation_key": observation_key, "options": options})
        result = (",".join(delivery.sent) if delivery.sent else
                  ("pending: " + ",".join(delivery.pending_channels)
                   if delivery.pending_channels else "no channel configured or duplicate"))
        publish_for(ctx, MasterEscalationNotified(
            run_id=state.body.run_id, fence=state.body.fence, ticket=state.ticket,
            actor=state.run["account_id"], result=result, reason=delivery.reason,
            options=list(delivery.options), observation_key=observation_key,
            pending_channels=list(delivery.pending_channels)))


def escalate(ctx, audit, state: MasterActionState, decision, log) -> dict:
    body = state.body
    options = escalation_options(body.options, defaults=False)
    if body.action == "escalate_to_owner" and (not body.why.strip() or not options):
        refuse(ctx, audit, state, "escalate_to_owner requires a reason and available options", 400)
    if not options:
        options = escalation_options([])
    reason = (body.why or f"The policy requires a person to decide: {decision.reason}").strip()[:1500]
    ticket_updated = None
    if not body.observation_key.strip() and not (body.kind and body.subject) and state.ticket:
        ticket_updated = ctx.db.one("SELECT jira_updated FROM tickets WHERE key=%s", (state.ticket,))
    observation_key = observation_key_for(body, state.ticket, ticket_updated)
    record = record_escalation(ctx, state, reason, options, observation_key)
    delivery = deliver_escalation(ctx, state, record, state.policy_project, log)
    publish_escalation(ctx, audit, state, decision, observation_key, options, delivery)
    master_action_event(ctx, state, "escalated", delivery.reason)
    return {"ok": True, "escalated": True, "reason": delivery.reason, "options": list(delivery.options),
            "already_sent": delivery.already_sent, "sent": list(delivery.sent),
            "pending_channels": list(delivery.pending_channels)}


def validate_ticket_action(ctx, audit, state: MasterActionState):
    if state.body.action not in {"retry", "move_to_todo", "restart_run"}:
        return None
    ticket_row = ctx.db.one("SELECT key, project, status FROM tickets WHERE key=%s", (state.ticket,)) \
        if state.ticket else None
    if not ticket_row:
        reason = "a valid Jira ticket is required for this master action"
        audit("master", state.body.action, state.ticket, state.body.run_id, state.body.fence,
              state.run["account_id"], False, reason)
        master_action_event(ctx, state, "denied", reason)
        raise HTTPException(400, reason)
    if not ctx.jira:
        refuse(ctx, audit, state, "Jira is unavailable", 503)
    return ticket_row


def retry_or_move(ctx, audit, state: MasterActionState, ticket_row: dict[str, Any]) -> str:
    body = state.body
    if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
        refuse(ctx, audit, state, f"a ticket in {ticket_row['status']} cannot be retried by the master")
    if body.action == "move_to_todo" and ticket_row["status"] not in ("To Do", "In Progress", "In Verify"):
        refuse(ctx, audit, state, "move_to_todo is limited to tickets in To Do, In Progress or In Verify")
    if ctx.leases.holder(state.ticket):
        refuse(ctx, audit, state, "ticket still has an active run; use restart_run after its work-time limit")
    if body.action == "retry":
        latest = ctx.db.one("SELECT outcome FROM runs WHERE ticket_key=%s AND status<>'running'"
                            " ORDER BY started_at DESC LIMIT 1", (state.ticket,))
        if not latest or latest["outcome"] not in ("failed", "timeout", "lost", "quota", "blocked", "no_changes"):
            refuse(ctx, audit, state, "retry requires a recent failed, blocked, quota or lost run")
    try:
        status = ctx.jira.transition_to(state.ticket, "To Do")
    except Exception as exc:  # noqa: BLE001 - preserve the refusal without exposing Jira internals
        refuse(ctx, audit, state, f"Jira could not move {state.ticket} to To Do ({type(exc).__name__})", 502)
    ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, state.ticket))
    return f"ticket moved to {status}"


def pause_account(ctx, audit, state: MasterActionState) -> str:
    body = state.body
    account_id = body.account_id.strip()
    account = ctx.db.one("SELECT id, status FROM accounts WHERE id=%s", (account_id,)) if account_id else None
    if not account:
        refuse(ctx, audit, state, "pause_account requires a configured account_id", 400)
    unhealthy = account["status"] in ("cooldown", "signed_out", "error") or bool(
        ctx.db.one("SELECT 1 AS issue FROM incidents WHERE closed_at IS NULL"
                   " AND kind IN ('account_quota','quota_high') AND (subject=%s OR subject LIKE %s)",
                   (account_id, account_id + ":%")))
    if not unhealthy:
        refuse(ctx, audit, state, "master may pause only an account with a quota or health issue")
    minutes = min(1440, max(1, int(body.pause_minutes)))
    ctx.db.run("INSERT INTO master_account_pauses (account_id,paused_until,reason,run_id)"
               " VALUES (%s,now()+(%s * interval '1 minute'),%s,%s) ON CONFLICT (account_id) DO UPDATE SET"
               " paused_until=EXCLUDED.paused_until,reason=EXCLUDED.reason,run_id=EXCLUDED.run_id,updated_at=now()",
               (account_id, minutes, (body.why or body.intent)[:500], body.run_id))
    return f"account paused for {minutes} minutes"


def require_restart_target(ctx, audit, state: MasterActionState, ticket_row: dict[str, Any]) -> dict[str, Any]:
    body = state.body
    if not body.target_run_id or not ctx.jira:
        refuse(ctx, audit, state, "restart_run requires target_run_id and an available Jira connection", 400)
    target = ctx.db.one("SELECT * FROM runs WHERE id::text=%s", (body.target_run_id,))
    if not target:
        refuse(ctx, audit, state, "target_run_id does not identify a recorded run", 404)
    if (target["ticket_key"] != state.ticket or target["step"] == "master" or target["status"] != "running"
            or not ctx.leases.valid(target["ticket_key"], str(target["id"]), int(target["fence"]))):
        refuse(ctx, audit, state, "target run is not the active fenced run for this ticket")
    if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
        refuse(ctx, audit, state, f"a run on a ticket in {ticket_row['status']} cannot be restarted by the master")
    return target


def restart_run(ctx, audit, state: MasterActionState, ticket_row: dict[str, Any]) -> str:
    body = state.body
    target = require_restart_target(ctx, audit, state, ticket_row)
    project = ctx.repo().projects.get(target["project"])
    hard = project.work_minutes()["hard"] if project else work_limits.HARD
    elapsed = max(0.0, (utcnow() - target["started_at"]).total_seconds() / 60)
    if elapsed < hard:
        refuse(ctx, audit, state, f"target run has not passed its {hard} minute work-time limit")
    if not ctx.leases.release(target["ticket_key"], str(target["id"]), int(target["fence"])):
        refuse(ctx, audit, state, "target run lease changed before restart")
    run_inbox_control.restart(ctx, target, body.target_run_id, body.run_id, body.why or body.intent)
    ctx.db.run("UPDATE accounts SET status='idle',updated_at=now() WHERE id=%s AND status='working'",
               (target["account_id"],))
    try:
        status = ctx.jira.transition_to(state.ticket, "To Do")
    except Exception as exc:  # noqa: BLE001 - the old lease is already fenced off
        reason = (f"run {body.target_run_id} was stopped, but Jira could not move {state.ticket} to To Do "
                  f"({type(exc).__name__})")
        audit("master", body.action, state.ticket, body.run_id, body.fence, state.run["account_id"], True, reason,
              {"intent": body.intent[:1000], "why": body.why[:1000], "target_run_id": body.target_run_id})
        master_action_event(ctx, state, "partial", reason)
        raise HTTPException(502, reason) from exc
    ctx.db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, state.ticket))
    ticket_row["project"] = target["project"]
    return f"stuck run {body.target_run_id} stopped; ticket moved to {status}"


def open_bug(ctx, audit, state: MasterActionState, process_flow_bugs) -> tuple[str, str]:
    candidates = {candidate["signature"] for candidate in flow_bug_candidates(ctx)
                  if candidate["creation_eligible"]}
    if not state.body.signature or state.body.signature not in candidates:
        refuse(ctx, audit, state, "open_bug requires an eligible repeated flow failure signature")
    reports = process_flow_bugs(state.run)
    matching = next((report for report in reports if report.get("signature") == state.body.signature), None)
    if not matching:
        tracked = ctx.db.one("SELECT ticket_key FROM flow_bug_tracker WHERE signature=%s", (state.body.signature,))
        matching = {"action": "already_reported", "ticket": tracked["ticket_key"]} if tracked else None
    if not matching:
        refuse(ctx, audit, state,
               "the eligible flow failure could not be recorded; it will be retried next cycle", 502)
    ticket = matching.get("ticket", "")
    return f"{matching['action']} {ticket}".strip(), ticket


