"""Control services for assembling triage context and guarding owner-escalated development claims."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from . import escalations
from . import ticket_lifecycle
from .repositories import triage_claim as repository


@dataclass(frozen=True)
class EligibilityRule:
    name: str
    check: Callable[[object, dict], bool]

def last_dev(ctx, ticket: str, done: bool = False) -> dict | None:
    """Return the latest completed development run used by claim eligibility and context."""
    return repository.last_dev(ctx.db, ticket, done=done)


def context(ctx, ticket: dict) -> dict:
    """Assemble the context sent to a triage worker from repository and Jira data."""
    key = ticket["key"]
    result = _base_context(ctx, ticket, key)
    _add_reviewer_question(ctx, key, result)
    _add_pull_request(ctx, key, result)
    return result


def _base_context(ctx, ticket: dict, key: str) -> dict:
    verify = repository.latest_verify(ctx.db, key)
    raised = repository.open_escalation(ctx.db, key)
    return {
        "last_dev_summary": (repository.last_dev(ctx.db, key) or {}).get("summary", ""),
        "verify_findings": ((verify["details"] or {}).get("findings") or verify["summary"]) if verify else "",
        "recent_comments": ctx.jira.recent_comments(key, limit=10) if ctx.jira else [],
        "owner_register": [{"id": row["id"], "title": row["title"], "decision": row["decision"]}
                           for row in repository.owner_register(ctx.db, ticket["project"])],
        "escalation_class": (raised or {}).get("class", ""),
        "triage_question": (raised or {}).get("question", ""),
    }


def _add_reviewer_question(ctx, key: str, result: dict) -> None:
    asked = repository.latest_review_question(ctx.db, key)
    prior_triage = repository.latest_triage(ctx.db, key)
    if asked and (not prior_triage or asked["ts"] > prior_triage["started_at"]):
        question = str((asked.get("data") or {}).get("text") or "").strip()
        if question:
            result["last_dev_summary"] = f"Reviewer question: {question}"
            result["triage_question"] = question


def _add_pull_request(ctx, key: str, result: dict) -> None:
    prior_pr = repository.latest_pr(ctx.db, key)
    match = re.search(r"/pull/(\d+)(?:/|$)", (prior_pr or {}).get("pr_url", ""))
    if match:
        result["pr"] = {"url": prior_pr["pr_url"], "number": int(match.group(1))}


def authorize_development(ctx, ticket: dict) -> tuple[bool, dict | None]:
    """Require an owner-authored Jira answer for every unresolved owner escalation before creating a dev run."""
    key = ticket["key"]
    state = repository.owner_answer_state(ctx.db, key)
    if not state["pending"]:
        if state["verified"]:
            if "owner-escalated" in state["labels"] and ctx.jira:
                escalations.clear_owner_labels(ctx, key)
            return True, _answer(state["verified"], key)
        if "owner-escalated" not in state["labels"]:
            return True, None
        _keep_blocked(ctx, ticket)
        return False, None

    answer = escalations.owner_answered_on_ticket(ctx, key)
    if answer:
        return True, answer
    # A concurrent claim may have persisted the verified answer after the first state read.
    state = repository.owner_answer_state(ctx.db, key)
    if not state["pending"] and state["verified"]:
        return True, _answer(state["verified"], key)
    _keep_blocked(ctx, ticket)
    return False, None


def _answer(row: dict, ticket: str) -> dict:
    return {"decision": {"id": row["id"]}, "answer": row["decision"], "question": row["question"],
            "ticket": ticket}


def _keep_blocked(ctx, ticket: dict) -> None:
    """Return a prematurely requeued owner escalation to its blocked Jira status."""
    ticket_lifecycle.transition(ctx, ticket, "owner-answer-required")


def _owner_answer_eligible(ctx, ticket: dict) -> bool:
    allowed, _answer = authorize_development(ctx, ticket)
    return allowed


DEVELOPMENT_ELIGIBILITY_PIPELINE = (
    EligibilityRule("verified-owner-answer", _owner_answer_eligible),
)


def development_eligible(ctx, ticket: dict) -> bool:
    """Run the named development eligibility rules in registration order."""
    return all(rule.check(ctx, ticket) for rule in DEVELOPMENT_ELIGIBILITY_PIPELINE)


def filter_candidates(ctx, candidates: list[dict]) -> list[dict]:
    """Apply development guards before candidate polling, routing, or claiming."""
    return [ticket for ticket in candidates
            if ticket.get("step") != "dev" or development_eligible(ctx, ticket)]
