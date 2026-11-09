"""Dashboard list of tickets waiting for verification longer than 30 minutes (JAIF-444 / JAIF-271 AC7).

`/v1/state` carries `slow_verifications` and a per-ticket `slow_verify` line. Waiting starts when the latest
successful developer run ends (or from Jira sync times when none exists). Account history is the current
development round, matching the no-verdict bar.
"""
from __future__ import annotations

from .repositories.runs import verify_ticket_history
from .repositories.slow_verifications import database_now, waiting_verifications
from .verify_no_verdict import barred

SLOW_MINUTES = 30


def _account_history(history: dict, key: str) -> dict[str, list[bool]]:
    ticket = history.get(key)
    return ticket.by_account if ticket else {}


def slow_verifications(db, minutes: int = SLOW_MINUTES, now=None) -> list[dict]:
    """Tickets waiting for verification longer than `minutes`, with the accounts that tried."""
    checked_at = now if now is not None else database_now(db)
    waiting = waiting_verifications(db, minutes, now=checked_at)
    history = verify_ticket_history(db, [ticket["key"] for ticket in waiting])
    out = []
    for ticket in waiting:
        tried = []
        for account, runs in _account_history(history, ticket["key"]).items():
            tried.append({"account": account, "runs": len(runs), "no_verdict": int(sum(runs)),
                          "barred": barred(runs)})
        out.append({"key": ticket["key"], "project": ticket["project"], "summary": ticket["summary"],
                    "waiting_minutes": int((checked_at - ticket["since"]).total_seconds() // 60),
                    "accounts": sorted(tried, key=lambda item: item["account"])})
    return out


def describe(slow: dict) -> str:
    """One line for the Dashboard: how long the ticket waits and which accounts tried."""
    tried = []
    for account in slow["accounts"]:
        label = account["account"]
        if account["no_verdict"]:
            label += (f" (no verdict x{account['no_verdict']}"
                      + (", not offered again)" if account["barred"] else ")"))
        tried.append(label)
    return f"{slow['waiting_minutes']} min · tried: {', '.join(tried) or 'no account has tried yet'}"


def attach_slow_verifications(db, tickets: list[dict]) -> list[dict]:
    """Attach each slow-verification summary to its ticket and return the complete Dashboard list."""
    slow = slow_verifications(db)
    by_ticket = {item["key"]: item for item in slow}
    for ticket in tickets:
        item = by_ticket.get(ticket["key"])
        ticket["slow_verify"] = describe(item) if item else None
    return slow


def redact_account_ids(slow: list[dict]) -> None:
    """Replace verifier account ids with Verifier N labels for callers without machine access."""
    for item in slow:
        for index, account in enumerate(item.get("accounts", []), start=1):
            account["account"] = f"Verifier {index}"


def scope_and_redact(data: dict, visible_projects: set[str] | None, reveal_accounts: bool) -> None:
    """Filter slow verifications by project and optionally hide verifier identities."""
    slow = data.get("slow_verifications")
    if slow is None:
        return
    if visible_projects is not None:
        slow[:] = [item for item in slow if item.get("project") in visible_projects]
    if reveal_accounts:
        return
    redact_account_ids(slow)
    by_ticket = {item["key"]: item for item in slow}
    for ticket in data.get("tickets", []):
        item = by_ticket.get(ticket["key"])
        if item:
            ticket["slow_verify"] = describe(item)
        elif "slow_verify" in ticket:
            ticket["slow_verify"] = None
