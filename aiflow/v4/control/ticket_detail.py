"""Assemble a ticket drawer from ticket, run, event and audit repositories."""
from __future__ import annotations

from ..core.controlrepo import epic_for_ticket
from . import merge_flow
from .identity_redaction import redact_account_ids_in_text
from .repositories.audits import AuditsRepository
from .repositories.events import EventsRepository
from .repositories.runs import RunsRepository
from .repositories.tickets import TicketsRepository
from .timeline import timeline


class TicketNotFound(LookupError):
    """Raised when a drawer request names a ticket that is not mirrored locally."""


def ticket_detail(ctx, key: str) -> dict:
    tickets = TicketsRepository(ctx.db)
    ticket, ancestors = tickets.with_ancestors(key)
    if not ticket:
        raise TicketNotFound(key)
    ticket["epic"] = epic_for_ticket(ticket, {item["key"]: item for item in ancestors})
    project = ctx.repo().projects.get(ticket["project"])
    ticket["repository"] = project.repo_for(ticket) if project else ""
    runs, capacity_decision = _run_details(RunsRepository(ctx.db), key)
    return {
        "ticket": ticket,
        "repository": ticket["repository"],
        "runs": runs,
        "events": EventsRepository(ctx.db).ticket_detail_rows(key),
        "writes": AuditsRepository(ctx.db).ticket_writes(key),
        "timeline": timeline(ctx.db, key),
        "capacity_decision": capacity_decision,
        "merge": merge_flow.facts(ticket),
        "moves": merge_flow.moves(ctx.db, key),
        "jira_url": f"{ctx.settings.jira_site}/browse/{key}",
    }


def redact_ticket_identities(data: dict) -> dict:
    """Replace account, machine and audit actor identities throughout a ticket response."""
    redact_account_ids_in_text(data)
    _redact_account_fields(data)
    decision = data.get("capacity_decision")
    if decision:
        decision["reason"] = "Assignment rationale is restricted for this role."
    return data


def _redact_account_fields(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"account", "account_id"} and item:
                value[key] = "Verifier"
            elif key == "actor" and item:
                value[key] = "Actor"
            elif key in {"node", "machine", "machine_id"} and item:
                value[key] = "Machine"
            else:
                _redact_account_fields(item)
    elif isinstance(value, list):
        for item in value:
            _redact_account_fields(item)


def _run_details(runs_repository: RunsRepository, key: str) -> tuple[list[dict], dict | None]:
    runs = runs_repository.ticket_detail_rows(key)
    for run in runs:
        run["id"] = str(run["id"])
        run["cost_usd"] = float(run["cost_usd"])
    decision = runs_repository.latest_capacity_decision(key)
    capacity = None if not decision else {
        "account": decision["account_id"],
        "provider": decision["provider"],
        "assigned_at": decision["started_at"].isoformat(),
        "estimate": decision.get("estimate") or {},
        "reason": decision.get("reason") or "",
        "account_fit": decision.get("account_fit") or {},
    }
    return runs, capacity
