"""Verification runs that end without a verdict (docs/v4/verify-no-verdict.md).

A verify run whose answer has no `VERDICT:` line is stored with `details.no_verdict = true`. This module decides
what happens next, by rule and without AI (queries live in `repositories/verify_runs.py`):

* The ticket is retried at once (no backoff) on an account that has not ended this ticket without a verdict. The
  same account is asked again only when no other eligible account has still to try.
* An account with two consecutive no-verdict results on the same ticket is not offered that ticket again, and
  role health counts a no-verdict result against the verify role only from that second result on.
* When every eligible account is barred the watchdog raises one `verify_no_verdict` incident for the ticket.
* The Dashboard lists tickets waiting for verification longer than 30 minutes with the accounts that tried.

All of it is scoped to the current development round: a newer finished dev run starts the count again.
"""
from __future__ import annotations

from fastapi import HTTPException

from . import role_health, shared_accounts
from .incidents import close_incident, open_incident
from .repositories.verify_runs import VerifyRunsRepository

KIND = "verify_no_verdict"
SLOW_MINUTES = 30
CONSECUTIVE = 2


def is_epic(ticket: dict) -> bool:
    return str(ticket.get("type") or "").strip().lower() == "epic"


def cooling(db) -> set[str]:
    """Verify tickets still waiting out the shared backoff after a failed run (no-verdict runs retry at once)."""
    return VerifyRunsRepository(db).cooling_tickets("verify")


def history(db, ticket_key: str) -> dict[str, list[bool]]:
    """Per account, newest first: did each verify run of this development round end without a verdict."""
    out: dict[str, list[bool]] = {}
    for row in VerifyRunsRepository(db).round_runs(ticket_key):
        out.setdefault(row["account_id"], []).append(bool(row["no_verdict"]))
    return out


def barred(runs: list[bool]) -> bool:
    """Two consecutive no-verdict results (newest first) bar the account from the ticket."""
    return len(runs) >= CONSECUTIVE and all(runs[:CONSECUTIVE])


def pool(ctx, ticket: dict, dev_account: str, runs: dict[str, list[bool]] | None = None) -> list[str]:
    """Accounts eligible to verify the ticket: the verify role, not paused, not the author, label rules.

    An account that is not barred also needs a machine that is online and accepting work. An offline account
    cannot receive the ticket, so it must not keep the ticket from counting as exhausted.
    """
    runs = history(ctx.db, ticket["key"]) if runs is None else runs
    online = shared_accounts.online_nodes(ctx.db)
    accepting = VerifyRunsRepository(ctx.db).accepting_nodes()
    labels = set(ticket.get("labels") or [])
    out = []
    for acc in ctx.repo().accounts:
        if acc.paused or acc.id == dev_account or "verify" not in role_health.account_roles(acc):
            continue
        if acc.explicit_only and f"ai-{acc.provider}" not in labels:
            continue
        if role_health.modes(ctx.db, acc.id, ["verify"])["verify"] == "off":
            continue
        if not barred(runs.get(acc.id, [])) and not any(m in online and m in accepting for m in acc.machines):
            continue
        out.append(acc.id)
    return out


def may_take(ctx, ticket: dict, account_id: str, dev_account: str, offers) -> bool:
    """Whether `account_id` may be offered the ticket's verification now.

    `offers(account, node)` is the final candidate path of the control API (entitlement, rollout and the
    20-ticket limit included): an untried account holds the ticket back only when that path really offers it
    this ticket. `offers=None` skips the deferral (used while that path is already being evaluated).
    """
    runs = history(ctx.db, ticket["key"])
    if barred(runs.get(account_id, [])):
        return False
    if offers is None or not any(runs.get(account_id, [])):  # never ended this ticket without a verdict
        return True
    online = shared_accounts.online_nodes(ctx.db)
    accounts = {acc.id: acc for acc in ctx.repo().accounts}
    for other in pool(ctx, ticket, dev_account, runs):
        if other == account_id or barred(runs.get(other, [])) or any(runs.get(other, [])):
            continue
        for machine in dict.fromkeys(accounts[other].machines):
            if machine in online and _offered(offers, other, machine, ticket["key"]):
                return False  # an eligible account has not tried yet: it goes first
    return True


def _offered(offers, account_id: str, node: str, key: str) -> bool:
    try:
        return any(t["key"] == key and t.get("step") == "verify" for t in offers(account_id, node))
    except HTTPException:
        return False


def exhausted(ctx, ticket: dict) -> list[str]:
    """The accounts that failed the ticket when every eligible account is barred, else an empty list."""
    runs = history(ctx.db, ticket["key"])
    accounts = pool(ctx, ticket, VerifyRunsRepository(ctx.db).last_dev_account(ticket["key"]), runs)
    if not accounts or not all(barred(runs.get(account, [])) for account in accounts):
        return []
    return sorted(accounts)


def check_incidents(ctx) -> None:
    """Watchdog: one incident per ticket whose eligible accounts all ended verification without a verdict."""
    repo = VerifyRunsRepository(ctx.db)
    open_now = set()
    for ticket in repo.tickets_in_verify():
        failed = exhausted(ctx, ticket)
        if not failed:
            continue
        open_now.add(ticket["key"])
        open_incident(ctx, KIND, ticket["key"], "warning",
                      f"{ticket['key']} cannot be verified: every eligible account ended without a verdict",
                      f"Reason: the verification of {ticket['key']} ended without a VERDICT line twice in a row on each"
                      f" eligible account ({', '.join(failed)}). None of them is offered the ticket again. Fix the"
                      " cause or verify the ticket by hand.")
    for subject in repo.open_incident_subjects(KIND):
        if subject not in open_now:
            close_incident(ctx, KIND, subject, "the ticket can be verified again or left verification")


def slow_verifications(db, minutes: int = SLOW_MINUTES) -> list[dict]:
    """Tickets waiting for verification longer than `minutes`, with the accounts that tried."""
    repo = VerifyRunsRepository(db)
    out = []
    for ticket in repo.waiting_tickets(minutes):
        tried = []
        for account, runs in history(db, ticket["key"]).items():
            tried.append({"account": account, "runs": len(runs), "no_verdict": sum(runs), "barred": barred(runs)})
        out.append({"key": ticket["key"], "project": ticket["project"], "summary": ticket["summary"],
                    "waiting_minutes": int((repo.now() - ticket["since"]).total_seconds() // 60),
                    "accounts": sorted(tried, key=lambda item: item["account"])})
    return out


def describe(slow: dict) -> str:
    """One line for the Dashboard: how long the ticket waits and which accounts tried."""
    tried = ", ".join(a["account"] + (f" (no verdict x{a['no_verdict']}" + (", not offered again)" if a["barred"] else ")")
                                       if a["no_verdict"] else "") for a in slow["accounts"])
    return f"{slow['waiting_minutes']} min · tried: {tried or 'no account has tried yet'}"
