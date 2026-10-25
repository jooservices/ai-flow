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
    now, found = state["now"], []
    thresholds = state["thresholds"]
    blocked_minutes = thresholds["blocked_minutes"]
    stuck_minutes = thresholds["stuck_minutes"]
    ci_wait_minutes = thresholds["ci_wait_minutes"]
    rework_limit = thresholds["rework_limit"]
    for ticket in state.get("tickets", []):
        key, status, labels = ticket["key"], ticket["status"], ticket.get("labels", [])
        if "aiflow-hold" in labels or "needs-owner" in labels:
            continue
        status_since = ticket.get("status_since", ticket.get("updated", now))
        minutes = max(0, (now - status_since) / 60)
        pull_request = ticket.get("pull_request")
        facts = {
            "ticket": key,
            "status": status,
            "times": {
                "detected_at": now,
                "status_since": status_since,
                "minutes_in_status": int(minutes),
                "ci_since": ticket.get("ci_since"),
                "minutes_waiting_for_ci": ticket.get("minutes_waiting_for_ci"),
            },
            "labels": labels,
            "last_comments": ticket.get("last_comments", []),
            "pull_request": pull_request,
            "ci_state": ticket.get("ci_state", "unknown"),
        }
        blocked_kind = ticket.get("blocked_kind", "")
        verdict = master_rescue.evaluate(ticket, state.get("master"), minutes)
        if verdict:  # the master takes the ticket before the generic blocked / stuck incidents
            found.append(Incident(f"rescue:{key}", "rescue", key, f"{key} qualifies for a master rescue: {verdict.trigger}",
                                  {**facts, "blocked_kind": blocked_kind, "reason": ticket.get("reason", ""), "summary": ticket.get("summary", ""),
                                   "mode": verdict.mode, "trigger": verdict.trigger, "topic": verdict.topic,
                                   "stretch_minutes": int(ticket.get("stretch_minutes", 0)),
                                   "worked_minutes": int(ticket.get("worked_minutes", 0)),
                                   "remaining_minutes": verdict.remaining_minutes}))
            continue
        pr_state = pull_request.get("state", "") if isinstance(pull_request, dict) else ""
        ci_waiting = (status == "In QA" and not ticket.get("leased") and pull_request
                      and pr_state in ("", "OPEN") and ticket.get("ci_state") in ("none", "pending"))
        rounds = max(ticket.get("cycles", 0), ticket.get("rework_rounds", 0))
        limit = ticket.get("loop_limit", rework_limit)
        rework_over_limit = rounds > limit
        if (status == "Blocked" and blocked_kind not in RETURNS_BY_ITSELF
                and not (blocked_kind == "Rework" and rework_over_limit)
                and minutes >= blocked_minutes):
            found.append(Incident(f"blocked:{key}", "blocked", key, f"{key} is Blocked for {int(minutes)} min: {ticket.get('reason', '')}",
                                  {**facts, "blocked_kind": blocked_kind, "reason": ticket.get("reason", "")}))
        elif (ci_waiting
              and ticket.get("minutes_waiting_for_ci", 0) >= ci_wait_minutes):
            found.append(Incident(f"ci:{key}", "ci", key,
                                  f"{key} pull request is waiting for CI for {int(ticket['minutes_waiting_for_ci'])} min", facts))
        elif (status in WORKED and not ticket.get("leased") and minutes >= stuck_minutes and not ci_waiting):
            found.append(Incident(f"stuck:{key}", "stuck", key, f"{key} has been in {status} for {int(minutes)} min with nobody working on it", facts))
        if rework_over_limit:
            found.append(Incident(f"rework:{key}", "rework", key, f"{key} went through {rounds} rework rounds",
                                  {**facts, "rework_rounds": rounds}))
    for slot, beat in sorted(state.get("heartbeats", {}).items()):
        if beat is None or now - beat > state["stale_seconds"]:
            found.append(Incident(f"worker:{slot}", "worker", slot, f"worker {slot} sends no heartbeat",
                                  {"slot": slot, "last_heartbeat": beat, "detected_at": now, "stale_seconds": state["stale_seconds"]}))
    for role, pool in sorted(state.get("role_pools", {}).items()):
        if not pool:
            found.append(Incident(f"ai_pool:{role}", "ai_pool", role, f"no AI is usable for the {role} role",
                                  {"role": role, "pool": [], "detected_at": now}))
    return found
