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

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import HTTPException

from . import role_health
from .incidents import close_incident, open_incident
from .repositories.incidents import IncidentsRepository
from .repositories.tickets import TicketsRepository
from .repositories.verify_runs import VerifyRunsRepository
from . import work_eligibility

KIND = "verify_no_verdict"
SLOW_MINUTES = 30
CONSECUTIVE = 2
VERIFY_RULES = ("verify_ticket_not_held", "verify_ticket_not_cooling", "verify_machine_ready",
                "verify_has_other_developer_pr", "verify_provider_label", "verify_role_enabled",
                "verify_retry_account")


@dataclass(frozen=True)
class VerifyCandidateState:
    ctx: object
    repository: VerifyRunsRepository
    account: object
    held: set[str]
    cooling: set[str]
    offers: Callable
    can_handle: Callable
    allowed: Callable
    retry_guard: bool


def _reject_verify_held(state: VerifyCandidateState, ticket: dict, _facts: dict) -> str | None:
    return "ticket has an active lease" if ticket["key"] in state.held else None


def _reject_verify_cooling(state: VerifyCandidateState, ticket: dict, _facts: dict) -> str | None:
    return "ticket is cooling down after an ordinary verify failure" \
        if ticket["key"] in state.cooling else None


def _reject_verify_machine(state: VerifyCandidateState, ticket: dict, _facts: dict) -> str | None:
    return "account cannot run the verification checks for this ticket" \
        if not state.can_handle(ticket) else None


def _reject_verify_author(state: VerifyCandidateState, ticket: dict, _facts: dict) -> str | None:
    dev = state.repository.latest_dev_with_pr(ticket["key"])
    if not dev or not dev["pr_url"]:
        return "ticket has no completed development pull request to verify"
    return "the development account cannot verify its own ticket" if dev["account_id"] == state.account.id else None


def _reject_verify_provider(state: VerifyCandidateState, ticket: dict, _facts: dict) -> str | None:
    required = f"ai-{state.account.provider}"
    return f"ticket does not carry the required provider label {required}" \
        if state.account.explicit_only and required not in ticket.get("labels", []) else None


def _reject_verify_role(state: VerifyCandidateState, ticket: dict, _facts: dict) -> str | None:
    return "verify role is off or the ticket is not eligible for probation" \
        if not state.allowed("verify", ticket) else None


def _reject_verify_retry(state: VerifyCandidateState, ticket: dict, _facts: dict) -> str | None:
    dev = state.repository.latest_dev_with_pr(ticket["key"])
    if state.retry_guard and not may_take(
            state.ctx, ticket, state.account.id, dev["account_id"],
            lambda peer, node: state.offers(peer, node)):
        return "an eligible account that has not tried this ticket goes first"
    return None


def _register_verify_rules() -> None:
    for name, check in (("verify_ticket_not_held", _reject_verify_held),
                        ("verify_ticket_not_cooling", _reject_verify_cooling),
                        ("verify_machine_ready", _reject_verify_machine),
                        ("verify_has_other_developer_pr", _reject_verify_author),
                        ("verify_provider_label", _reject_verify_provider),
                        ("verify_role_enabled", _reject_verify_role),
                        ("verify_retry_account", _reject_verify_retry)):
        work_eligibility.register_candidate_rule(name, check)


_register_verify_rules()


def is_epic(ticket: dict) -> bool:
    return str(ticket.get("type") or "").strip().lower() == "epic"


def cooling(db) -> set[str]:
    """Verify tickets still waiting out the shared backoff after a failed run (no-verdict runs retry at once)."""
    return VerifyRunsRepository(db).cooling_tickets("verify")


def final_offers(ctx, account: str, node: str, resolve, compatible, releases: Path,
                 license_error) -> list[dict]:
    """Run the normal final candidate gates for a retry probe without re-entering its retry rule."""
    try:
        tickets, _reason = resolve(account, node, verify_retry_guard=False)
    except license_error:
        return []
    return tickets[:20] if compatible(ctx, node, releases)["compatible"] else []


def scope_dashboard(data: dict, visible_projects: list[str] | None, reveal_accounts: bool) -> None:
    """Apply project visibility and verifier-identity permissions to slow-verification dashboard data."""
    slow = data["slow_verifications"]
    if visible_projects is not None:
        visible = set(visible_projects)
        slow[:] = [item for item in slow if item.get("project") in visible]
    if reveal_accounts:
        return
    by_ticket = {item["key"]: item for item in slow}
    for item in slow:
        for index, account in enumerate(item.get("accounts", []), start=1):
            account["account"] = f"Verifier {index}"
    for ticket in data["tickets"]:
        item = by_ticket.get(ticket["key"])
        if item:
            ticket["slow_verify"] = describe(item)


def history(db, ticket_key: str) -> dict[str, list[bool]]:
    """Per account, newest first: did each verify run of this development round end without a verdict."""
    out: dict[str, list[bool]] = {}
    for row in VerifyRunsRepository(db).round_runs(ticket_key):
        out.setdefault(row["account_id"], []).append(bool(row["no_verdict"]))
    return out


def barred(runs: list[bool]) -> bool:
    """Two consecutive no-verdict results (newest first) bar the account from the ticket."""
    return len(runs) >= CONSECUTIVE and all(runs[:CONSECUTIVE])


def candidates(ctx, projects: list[str], account, held: set[str], offers, can_handle, allowed, probation,
               retry_guard: bool = True) -> list[dict]:
    """Build this account's verify offers; `retry_guard=False` probes ordinary eligibility without recursion."""
    repository = VerifyRunsRepository(ctx.db)
    state = VerifyCandidateState(ctx, repository, account, held, repository.cooling_tickets("verify"),
                                 offers, can_handle, allowed, retry_guard)
    out = []
    for ticket in TicketsRepository(ctx.db).verification_candidates(projects):
        decision = work_eligibility.evaluate_candidate(state, ticket, VERIFY_RULES)
        if not decision.eligible:
            continue
        out.append({**ticket, "step": "verify", "probation": probation("verify")})
    return out


def pool(ctx, ticket: dict, dev_account: str, offers=None, releases: Path | None = None) -> list[str]:
    """Accounts the complete candidate path would offer this ticket, regardless of no-verdict history."""
    offers = offers or getattr(ctx, "candidate_offers", None)
    if offers is None:
        return []
    out = []
    for acc in ctx.repo().accounts:
        if acc.paused or acc.id == dev_account or "verify" not in role_health.account_roles(acc):
            continue
        for node in dict.fromkeys(acc.machines):
            if _offered(offers, acc.id, node, ticket["key"]):
                out.append(acc.id)
                break
    return out


def may_take(ctx, ticket: dict, account_id: str, dev_account: str, offers) -> bool:
    """Whether `account_id` may be offered the ticket's verification now.

    `offers(account, node)` is the final candidate path without the retry rule. An untried account holds the ticket
    back only when that path really offers it this ticket. `offers=None` skips the deferral (used during that probe).
    """
    runs = history(ctx.db, ticket["key"])
    if barred(runs.get(account_id, [])):
        return False
    if offers is None or not any(runs.get(account_id, [])):  # never ended this ticket without a verdict
        return True
    for other in pool(ctx, ticket, dev_account, offers):
        if other == account_id or barred(runs.get(other, [])) or any(runs.get(other, [])):
            continue
        return False  # an eligible account has not tried yet: it goes first
    return True


def _offered(offers, account_id: str, node: str, key: str) -> bool:
    try:
        return any(t["key"] == key and t.get("step") == "verify" for t in offers(account_id, node))
    except HTTPException:
        return False


def exhausted(ctx, ticket: dict, offers=None, releases: Path | None = None) -> list[str]:
    """The accounts that failed the ticket when every eligible account is barred, else an empty list."""
    _accounts, failed, _runs = exhaustion_state(ctx, ticket, offers, releases)
    return failed


def exhaustion_state(ctx, ticket: dict, offers=None, releases: Path | None = None
                     ) -> tuple[list[str], list[str], dict[str, list[bool]]]:
    """Return eligible accounts, barred eligible accounts, and current-round history for incident recovery."""
    runs = history(ctx.db, ticket["key"])
    dev_account = VerifyRunsRepository(ctx.db).last_dev_account(ticket["key"])
    accounts = pool(ctx, ticket, dev_account, offers, releases)
    failed = sorted(accounts) if accounts and all(barred(runs.get(account, [])) for account in accounts) else []
    return accounts, failed, runs


def check_incidents(ctx, releases: Path | None = None) -> None:
    """Watchdog: one incident per ticket whose eligible accounts all ended verification without a verdict."""
    already_open = set(IncidentsRepository(ctx.db).open_subjects(KIND))
    open_now = set()
    for ticket in TicketsRepository(ctx.db).tickets_in_verify():
        accounts, failed, runs = exhaustion_state(ctx, ticket, releases=releases)
        if not failed:
            if ticket["key"] in already_open and not any(not barred(runs.get(account, []))
                                                         for account in accounts):
                # No eligible capacity is temporary, not a recovery. Keep the existing incident until the ticket
                # leaves verification or an unbarred account can actually receive it.
                open_now.add(ticket["key"])
            continue
        open_now.add(ticket["key"])
        open_incident(ctx, KIND, ticket["key"], "warning",
                      f"{ticket['key']} cannot be verified: every eligible account ended without a verdict",
                      f"Reason: the verification of {ticket['key']} ended without a VERDICT line twice in a row on each"
                      f" eligible account ({', '.join(failed)}). None of them is offered the ticket again. Fix the"
                      " cause or verify the ticket by hand.")
    for subject in already_open:
        if subject not in open_now:
            close_incident(ctx, KIND, subject, "the ticket can be verified again or left verification")


def slow_verifications(db, minutes: int = SLOW_MINUTES, now=None) -> list[dict]:
    """Tickets waiting for verification longer than `minutes`, with the accounts that tried."""
    repo = VerifyRunsRepository(db)
    checked_at = now or repo.now()
    out = []
    for ticket in TicketsRepository(db).waiting_verifications(minutes, now=checked_at):
        tried = []
        for account, runs in history(db, ticket["key"]).items():
            tried.append({"account": account, "runs": len(runs), "no_verdict": sum(runs), "barred": barred(runs)})
        out.append({"key": ticket["key"], "project": ticket["project"], "summary": ticket["summary"],
                    "waiting_minutes": int((checked_at - ticket["since"]).total_seconds() // 60),
                    "accounts": sorted(tried, key=lambda item: item["account"])})
    return out


def attach_slow_verifications(db, tickets: list[dict]) -> list[dict]:
    """Attach each slow-verification summary to its ticket and return the complete Dashboard list."""
    slow = slow_verifications(db)
    by_ticket = {item["key"]: item for item in slow}
    for ticket in tickets:
        item = by_ticket.get(ticket["key"])
        ticket["slow_verify"] = describe(item) if item else None
    return slow


def describe(slow: dict) -> str:
    """One line for the Dashboard: how long the ticket waits and which accounts tried."""
    tried = []
    for account in slow["accounts"]:
        label = account["account"]
        if account["no_verdict"]:
            label += f" (no verdict x{account['no_verdict']}" + (", not offered again)" if account["barred"] else ")")
        tried.append(label)
    return f"{slow['waiting_minutes']} min · tried: {', '.join(tried) or 'no account has tried yet'}"
