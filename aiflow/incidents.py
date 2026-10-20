"""Incidents (JAIF-72): what needs the operator. Pure: `find_incidents` takes facts and returns incidents, it reads nothing.

An incident has a stable id (`kind:subject`), so the same problem is queued once, and it disappears from the queue by itself when a
later check no longer finds it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .operator_policy import Policy

WORKED = ("In Progress", "In Review", "Review passed", "In QA", "Ready to merge")  # Human review waits for the owner by design
RETURNS_BY_ITSELF = ("Quota", "Offline")


@dataclass(frozen=True)
class Incident:
    id: str
    kind: str       # blocked | stuck | ci | worker | ai_pool | rework
    subject: str    # ticket key, worker slot or `all`
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ticket(self) -> str:
        return self.subject if "-" in self.subject else ""


def find_incidents(state: dict[str, Any], policy: Policy) -> list[Incident]:
    """`state`: now (epoch seconds); tickets [{key, status, updated (epoch), labels, leased, blocked_kind, reason, cycles}];
    heartbeats {slot: epoch or None}; stale_seconds; pool (usable AIs, any machine)."""
    now, found = state["now"], []
    for ticket in state.get("tickets", []):
        key, status, labels = ticket["key"], ticket["status"], ticket.get("labels", [])
        if "aiflow-hold" in labels:
            continue
        minutes = (now - ticket["updated"]) / 60
        facts = {"ticket": key, "status": status, "minutes_in_status": int(minutes), "labels": labels}
        escalated = "needs-owner" in labels
        if status == "Blocked" and not escalated and ticket.get("blocked_kind") not in RETURNS_BY_ITSELF and minutes >= policy.blocked_minutes:
            found.append(Incident(f"blocked:{key}", "blocked", key, f"{key} is Blocked for {int(minutes)} min: {ticket.get('reason', '')}",
                                  {**facts, "blocked_kind": ticket.get("blocked_kind", ""), "reason": ticket.get("reason", "")}))
        elif status == "In QA" and not ticket.get("leased") and minutes >= policy.ci_wait_minutes:
            found.append(Incident(f"ci:{key}", "ci", key, f"{key} has waited in QA for {int(minutes)} min", facts))
        elif status in WORKED and status != "In QA" and not ticket.get("leased") and minutes >= policy.stuck_minutes:
            found.append(Incident(f"stuck:{key}", "stuck", key, f"{key} has been in {status} for {int(minutes)} min with nobody working on it", facts))
        if ticket.get("cycles", 0) > policy.rework_limit and not escalated and status != "Blocked":
            found.append(Incident(f"rework:{key}", "rework", key, f"{key} went through {ticket['cycles']} rework rounds",
                                  {**facts, "rework_rounds": ticket["cycles"]}))
    for slot, beat in sorted(state.get("heartbeats", {}).items()):
        if beat is None or now - beat > state["stale_seconds"]:
            found.append(Incident(f"worker:{slot}", "worker", slot, f"worker {slot} sends no heartbeat", {"slot": slot, "last_heartbeat": beat}))
    if not state.get("pool"):
        found.append(Incident("ai_pool:all", "ai_pool", "all", "no AI is usable on any machine", {"pool": []}))
    return found
