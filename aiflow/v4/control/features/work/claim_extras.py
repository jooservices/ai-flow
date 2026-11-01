"""Per-step response extras and the final payload for a successful work claim."""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from ... import (claim_project, environment as environment_reports, escalations, knowledge, tooling)
from ....core import handover
from .claim_steps import Claim

__all__ = ["claim_project", "environment_reports", "escalations", "knowledge", "tooling"]

log = logging.getLogger("aiflow4.control")


def dev_extras(work: Claim) -> dict[str, Any]:
    """Resolve developer tools and add findings, branch and the latest answer to a blocked question."""
    ctx, ticket, store = work.ctx, work.ticket, work.store
    extra: dict[str, Any] = {}
    tooling.resolve_ticket(ctx, ticket["key"], work.node)
    extra["machine_tools"] = tooling.machine_tools(store.node_tools(work.node).as_row())
    dev = work.prior_dev
    latest = store.latest_rework(ticket["key"])
    if latest and (not dev or latest.started_at > dev["started_at"]):
        extra["findings"] = latest.details.get("findings") or latest.summary
    if dev and dev["branch"]:
        extra["branch"] = dev["branch"]
    answer = store.latest_answered_triage(ticket["key"])
    if answer and not store.dev_finished_since(ticket["key"], answer.started_at):
        question = store.blocked_question_before(ticket["key"], answer.started_at)
        extra["triage_answer"] = answer.details.get("answer") or answer.summary
        extra["triage_question"] = question
    automatic = escalations.pending_answer(ctx.db, ticket["key"])
    if automatic and (not extra.get("triage_answer") or not answer
                      or automatic["created_at"] > answer.started_at):
        extra["triage_answer"], extra["triage_question"] = automatic["answer"], automatic["question"]
    if work.owner_answer:
        extra["triage_answer"] = work.owner_answer["answer"]
        extra["triage_question"] = work.owner_answer["question"]
    return extra


def verify_extras(work: Claim) -> dict[str, Any]:
    """Return the pull request identity and developer summary for verification."""
    dev = work.prior_dev
    if not dev:
        return {}
    return {"pr": {"url": dev["pr_url"], "number": int(dev["pr_url"].rstrip("/").split("/")[-1]),
                    "branch": dev["branch"], "head_sha": dev["head_sha"], "dev_account": dev["account_id"],
                    "dev_summary": (dev["summary"] or "") + handover.render(dev.get("details"))}}


def triage_extras(work: Claim) -> dict[str, Any]:
    """Return the triage context gathered while selecting the ticket."""
    return dict(work.triage_context)


def post_merge_extras(work: Claim) -> dict[str, Any]:
    """Return the pending post-merge criteria for the claimed ticket."""
    ticket = work.ticket
    criteria = next((item["criteria"] for item in environment_reports.pending_post_merge(
        work.ctx, ticket["project"], [ticket["key"]])), [])
    return {"post_merge_criteria": criteria}


def intake_extras(work: Claim) -> dict[str, Any]:
    """Return recent owner comments, keeping comment lookup failure non-blocking."""
    try:
        comments = work.ctx.jira.recent_comments(work.ticket["key"], limit=10) if work.ctx.jira else []
        return {"recent_comments": comments}
    except Exception as exc:  # noqa: BLE001
        log.warning("comments of %s for intake failed: %s", work.ticket["key"], exc)
        return {}


def impact_extras(work: Claim) -> dict[str, Any]:
    """Return the project's open tickets for impact comparison."""
    tickets = [asdict(row) for row in work.store.open_project_tickets(work.ticket["project"], work.ticket["key"])]
    return {"project_tickets": tickets}


def master_extras(_work: Claim) -> dict[str, Any]:
    """Mark a master claim as having global scope."""
    return {"master_scope": "global"}


def related_extras(work: Claim) -> dict[str, Any]:
    """Return related memory hits for non-master work."""
    return {"related": work.deps.related_hits(work.ticket)}


def context_pack(work: Claim, extra: dict[str, Any]) -> None:
    """Add the knowledge context pack, falling back to an empty pack if local search fails."""
    ticket = work.ticket
    try:
        ticket_context = f"{ticket.get('summary', '')}\n{ticket.get('description', '')}"[:12_000]
        extra["context_pack"] = knowledge.context_pack(
            work.ctx.db, project=ticket.get("project", ""), ticket=ticket["key"],
            epic=work.epic or "", query=ticket_context, run_id=work.run_id)
    except Exception as exc:  # noqa: BLE001 - a local search failure cannot prevent a lease
        log.warning("knowledge context pack for %s failed: %s", ticket["key"], type(exc).__name__)
        extra["context_pack"] = {"project": ticket.get("project", ""), "ticket": ticket["key"], "items": []}


def final_response(work: Claim, extra: dict[str, Any]) -> dict[str, Any]:
    """Build the successful claim response with its project payload."""
    project_data = claim_project.payload(
        work.project, work.selected_repo, work.node, work.body.step, work.ctx.db, work.ticket["key"])
    return {**extra, "claimed": True, "step": work.body.step, "run_id": work.run_id,
            "fence": work.lease.fence, "run_token": work.run_token, "lease_ms": work.ctx.settings.lease_ms,
            "model": work.model, "effort": work.effort, "provider": work.account.provider,
            "account": work.account.id, "identity": work.deps.bot_identity(), "ticket": work.ticket_data,
            "project": project_data}
