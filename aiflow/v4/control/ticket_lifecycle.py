"""Table-driven ticket transitions and guards owned by the control plane."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .repositories import tickets as ticket_repository
from .repositories import triage_claim as claim_repository


@dataclass(frozen=True)
class Transition:
    target: str
    reason: str
    guard: Callable[[object, dict], bool]


def _owner_answer_required(ctx, ticket: dict) -> bool:
    state = claim_repository.owner_answer_state(ctx.db, ticket["key"])
    return bool(state["pending"] or "owner-escalated" in state["labels"])


def _verified_owner_answer(ctx, ticket: dict) -> bool:
    state = claim_repository.owner_answer_state(ctx.db, ticket["key"])
    return bool(state["verified"])


TRANSITIONS = {
    "owner-answer-required": Transition(
        target="Needs owner",
        reason="owner answer required before development",
        guard=_owner_answer_required,
    ),
    "verified-owner-answer": Transition(
        target="To Do",
        reason="owner answered in Jira after the question comment",
        guard=_verified_owner_answer,
    ),
}


def transition(ctx, ticket: dict, name: str) -> str:
    """Apply a registered, guarded lifecycle transition and keep Jira, mirror and event state aligned."""
    rule = TRANSITIONS.get(name)
    if rule is None:
        raise ValueError(f"unknown ticket transition: {name}")
    if not rule.guard(ctx, ticket):
        raise ValueError(f"ticket {ticket['key']} no longer meets the guard for {name}")
    if ctx.jira:
        status = ctx.jira.transition_to(ticket["key"], rule.target)
        if status != rule.target:
            raise RuntimeError(f"{ticket['key']} did not transition to {rule.target}")
    else:
        status = rule.target
    ticket_repository.update_status(ctx.db, ticket["key"], status)
    ctx.events.emit("jira.transition", ticket=ticket["key"], project=ticket["project"], account="control",
                    node="control", outcome=status, reason=rule.reason)
    return status
