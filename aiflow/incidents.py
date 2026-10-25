"""Incidents (JAIF-72): what needs the operator. Pure: `find_incidents` takes facts and returns incidents, it reads nothing.

An incident has a stable id (`kind:subject`), so the same problem is queued once, and it disappears from the queue by itself when a
later check no longer finds it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import master_rescue

WORKED = ("In Progress", "In Review", "Review passed", "In QA", "Ready to merge")  # Human review waits for the owner by design
RETURNS_BY_ITSELF = ("Quota", "Offline")


@dataclass(frozen=True)
class Incident:
    id: str
    kind: str       # blocked | stuck | ci | worker | ai_pool | rework | rescue
    subject: str    # ticket key, worker slot or `all`
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ticket(self) -> str:
        return self.subject if "-" in self.subject else ""


def find_incidents(state: dict[str, Any]) -> list[Incident]:
    """Find incidents from a snapshot of facts and thresholds; this function performs no I/O.

    `state` contains `now`, `thresholds`, tickets with status times, labels, lease, comments, pull-request and CI facts,
    worker heartbeats, and each role's usable AI pool.
    """
    now = state["now"]
    found = [incident for ticket in state.get("tickets", []) for incident in _ticket_incidents(ticket, state, now)]
    found.extend(_worker_incidents(state, now))
    found.extend(_role_pool_incidents(state, now))
    return found


def _ticket_incidents(ticket: dict[str, Any], state: dict[str, Any], now: float) -> list[Incident]:
    labels = ticket.get("labels", [])
    if "aiflow-hold" in labels or "needs-owner" in labels:
        return []
    status_since = ticket.get("status_since", ticket.get("updated", now))
    minutes = max(0, (now - status_since) / 60)
    facts = _ticket_facts(ticket, now, status_since, minutes)
    rescue = _rescue_incident(ticket, state, facts, minutes)
    return [rescue] if rescue else _standard_ticket_incidents(ticket, state, facts, minutes)


def _ticket_facts(ticket: dict[str, Any], now: float, status_since: float, minutes: float) -> dict[str, Any]:
    return {
        "ticket": ticket["key"],
        "status": ticket["status"],
        "times": {
            "detected_at": now,
            "status_since": status_since,
            "minutes_in_status": int(minutes),
            "ci_since": ticket.get("ci_since"),
            "minutes_waiting_for_ci": ticket.get("minutes_waiting_for_ci"),
        },
        "labels": ticket.get("labels", []),
        "last_comments": ticket.get("last_comments", []),
        "pull_request": ticket.get("pull_request"),
        "ci_state": ticket.get("ci_state", "unknown"),
    }


def _rescue_incident(ticket: dict[str, Any], state: dict[str, Any], facts: dict[str, Any], minutes: float) -> Incident | None:
    verdict = master_rescue.evaluate(ticket, state.get("master"), minutes)
    if not verdict:
        return None
    key = ticket["key"]
    return Incident(f"rescue:{key}", "rescue", key, f"{key} qualifies for a master rescue: {verdict.trigger}",
                    {**facts, "blocked_kind": ticket.get("blocked_kind", ""), "reason": ticket.get("reason", ""),
                     "summary": ticket.get("summary", ""), "mode": verdict.mode, "trigger": verdict.trigger,
                     "topic": verdict.topic, "stretch_minutes": int(ticket.get("stretch_minutes", 0)),
                     "worked_minutes": int(ticket.get("worked_minutes", 0)),
                     "remaining_minutes": verdict.remaining_minutes})


def _standard_ticket_incidents(ticket: dict[str, Any], state: dict[str, Any], facts: dict[str, Any], minutes: float) -> list[Incident]:
    rounds = max(ticket.get("cycles", 0), ticket.get("rework_rounds", 0))
    limit = ticket.get("loop_limit", state["thresholds"]["rework_limit"])
    primary = (_blocked_incident(ticket, state, facts, minutes, rounds, limit),
               _ci_incident(ticket, state, facts),
               _stuck_incident(ticket, state, facts, minutes))
    first = next((found for found in primary if found is not None), None)
    rework = _rework_incident(ticket, facts, rounds, limit)
    return [found for found in (first, rework) if found is not None]


def _blocked_incident(ticket: dict[str, Any], state: dict[str, Any], facts: dict[str, Any],
                      minutes: float, rounds: int, limit: int) -> Incident | None:
    kind = ticket.get("blocked_kind", "")
    if (ticket["status"] != "Blocked" or kind in RETURNS_BY_ITSELF
            or (kind == "Rework" and rounds > limit) or minutes < state["thresholds"]["blocked_minutes"]):
        return None
    key = ticket["key"]
    return Incident(f"blocked:{key}", "blocked", key, f"{key} is Blocked for {int(minutes)} min: {ticket.get('reason', '')}",
                    {**facts, "blocked_kind": kind, "reason": ticket.get("reason", "")})


def _ci_incident(ticket: dict[str, Any], state: dict[str, Any], facts: dict[str, Any]) -> Incident | None:
    wait = ticket.get("minutes_waiting_for_ci", 0)
    if not _ci_waiting(ticket) or wait < state["thresholds"]["ci_wait_minutes"]:
        return None
    key = ticket["key"]
    return Incident(f"ci:{key}", "ci", key, f"{key} pull request is waiting for CI for {int(wait)} min", facts)


def _stuck_incident(ticket: dict[str, Any], state: dict[str, Any], facts: dict[str, Any], minutes: float) -> Incident | None:
    if (ticket["status"] not in WORKED or ticket.get("leased") or minutes < state["thresholds"]["stuck_minutes"]
            or _ci_waiting(ticket)):
        return None
    key, status = ticket["key"], ticket["status"]
    return Incident(f"stuck:{key}", "stuck", key, f"{key} has been in {status} for {int(minutes)} min with nobody working on it", facts)


def _rework_incident(ticket: dict[str, Any], facts: dict[str, Any], rounds: int, limit: int) -> Incident | None:
    if rounds <= limit:
        return None
    key = ticket["key"]
    return Incident(f"rework:{key}", "rework", key, f"{key} went through {rounds} rework rounds",
                    {**facts, "rework_rounds": rounds})


def _ci_waiting(ticket: dict[str, Any]) -> bool:
    pull_request = ticket.get("pull_request")
    pr_state = pull_request.get("state", "") if isinstance(pull_request, dict) else ""
    return bool(ticket["status"] == "In QA" and not ticket.get("leased") and pull_request
                and pr_state in ("", "OPEN") and ticket.get("ci_state") in ("none", "pending"))


def _worker_incidents(state: dict[str, Any], now: float) -> list[Incident]:
    return [Incident(f"worker:{slot}", "worker", slot, f"worker {slot} sends no heartbeat",
                     {"slot": slot, "last_heartbeat": beat, "detected_at": now, "stale_seconds": state["stale_seconds"]})
            for slot, beat in sorted(state.get("heartbeats", {}).items())
            if beat is None or now - beat > state["stale_seconds"]]


def _role_pool_incidents(state: dict[str, Any], now: float) -> list[Incident]:
    return [Incident(f"ai_pool:{role}", "ai_pool", role, f"no AI is usable for the {role} role",
                     {"role": role, "pool": [], "detected_at": now})
            for role, pool in sorted(state.get("role_pools", {}).items()) if not pool]
