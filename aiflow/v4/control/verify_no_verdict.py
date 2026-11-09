"""One `verify_no_verdict` incident when every eligible account is barred from a ticket (JAIF-443 / JAIF-271 AC6).

When every account the verify candidate path would offer a ticket has two consecutive no-verdict results on that
ticket, the watchdog opens one warning that names the ticket and the reason. Repeated checks do not open a second
incident. Temporary capacity loss keeps an open incident; it closes when the ticket leaves verification or an
unbarred eligible account can receive it. Barring follows the current development round (JAIF-442): a new submission resets it.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

from fastapi import HTTPException

from . import role_health
from .features.work.eligible import eligible as eligible_tickets
from .features.work.repository import WorkRepository
from .incidents import close_incident, open_incident
from .repositories import verify_exhaustion
from .repositories.runs import verify_ticket_history
from .repositories.verify_exhaustion import last_dev_account, open_subjects, tickets_in_verify

KIND = "verify_no_verdict"  # also named in repositories/verify_exhaustion.py, which cannot import this module
CONSECUTIVE = 2
TITLE = "{key} cannot be verified: every eligible account ended without a verdict"
DETAIL = (
    "Reason: every eligible account ended verification of {key} without a VERDICT line"
    " twice in a row. None of them is offered the ticket again. Fix the"
    " cause or verify the ticket by hand."
)
CLOSE_NOTE = "the ticket can be verified again or left verification"


def barred(runs: list[bool]) -> bool:
    """Two consecutive no-verdict results (newest first) bar the account from the ticket."""
    return len(runs) >= CONSECUTIVE and all(runs[:CONSECUTIVE])


def ensure_offers(ctx, releases) -> Callable:
    """Return a final verify-offer probe; build one when control-loops has not installed create_app helpers."""
    existing = getattr(ctx, "candidate_offers", None)
    if existing is not None:
        return existing
    deps = _work_deps(ctx, releases)

    def offers(account_id: str, node: str, ticket_key: str | None = None) -> list[dict]:
        options = {"routing": False, "capacity_routing": False, "step_filter": "verify",
                   "verify_retry_guard": False}
        if ticket_key is not None:
            options["ticket_key"] = ticket_key
        try:
            return deps.eligible(account_id, node, **options)
        except HTTPException:
            return []

    ctx.candidate_offers = offers
    return offers


def _work_deps(ctx, releases) -> SimpleNamespace:
    """Minimal work-deps so `eligible()` can run from the loops process without FastAPI."""
    deps = SimpleNamespace()

    def eligible(account_id: str, node: str, **options) -> list[dict]:
        return eligible_tickets(ctx, deps, account_id, node, **options)

    deps.eligible = eligible
    deps.account_master_paused = lambda account_id: verify_exhaustion.account_master_paused(ctx.db, account_id)
    deps.last_dev = lambda key: WorkRepository(ctx.db).latest_dev_run(key)
    deps.quota_forecast_at_risk = lambda account_id: verify_exhaustion.quota_at_risk(ctx.db, account_id)
    deps.quota_peer_accounts = lambda account_id, roles=None: []
    deps.route_capacity = lambda account_id, node, offered, ticket_key=None: offered
    deps.ticket_epic = lambda ticket: ""
    deps.releases = lambda: releases
    return deps


def pool(ctx, ticket: dict, dev_account: str, offers: Callable) -> list[str]:
    """Accounts the verify candidate path would offer this ticket, ignoring no-verdict history."""
    out = []
    for account in ctx.repo().accounts:
        if account.paused or account.id == dev_account or "verify" not in role_health.account_roles(account):
            continue
        for node in dict.fromkeys(account.machines):
            if _offered(offers, account.id, node, ticket["key"]):
                out.append(account.id)
                break
    return out


def _offered(offers: Callable, account_id: str, node: str, key: str) -> bool:
    try:
        tickets = offers(account_id, node, key)
    except TypeError:
        tickets = offers(account_id, node)
    except HTTPException:
        return False
    return any(item["key"] == key and item.get("step") == "verify" for item in tickets)


def exhaustion_state(ctx, ticket: dict, offers: Callable
                     ) -> tuple[list[str], list[str], dict[str, list[bool]]]:
    """Eligible accounts, barred eligible accounts, and per-account no-verdict history."""
    history = verify_ticket_history(ctx.db, [ticket["key"]]).get(ticket["key"])
    runs = history.by_account if history else {}
    accounts = pool(ctx, ticket, last_dev_account(ctx.db, ticket["key"]), offers)
    failed = sorted(accounts) if accounts and all(barred(runs.get(account, [])) for account in accounts) else []
    return accounts, failed, runs


def exhausted(ctx, ticket: dict, offers: Callable | None = None, releases=None) -> list[str]:
    """Accounts that failed the ticket when every eligible account is barred, else an empty list."""
    from . import api as control_api
    offers = offers or ensure_offers(ctx, releases if releases is not None else control_api.RELEASES)
    _accounts, failed, _runs = exhaustion_state(ctx, ticket, offers)
    return failed


def check_incidents(ctx, releases=None) -> None:
    """Watchdog: one incident per ticket whose eligible accounts all ended verification without a verdict."""
    from . import api as control_api
    offers = ensure_offers(ctx, releases if releases is not None else control_api.RELEASES)
    already_open = open_subjects(ctx.db)
    open_now: set[str] = set()
    for ticket in tickets_in_verify(ctx.db):
        accounts, failed, runs = exhaustion_state(ctx, ticket, offers)
        if not failed:
            if ticket["key"] in already_open and not any(not barred(runs.get(account, []))
                                                         for account in accounts):
                # No eligible capacity is temporary, not a recovery. Keep the existing incident until the ticket
                # leaves verification or an unbarred account can actually receive it.
                open_now.add(ticket["key"])
            continue
        open_now.add(ticket["key"])
        open_incident(ctx, KIND, ticket["key"], "warning",
                      TITLE.format(key=ticket["key"]), DETAIL.format(key=ticket["key"]))
    for subject in already_open:
        if subject not in open_now:
            close_incident(ctx, KIND, subject, CLOSE_NOTE)
