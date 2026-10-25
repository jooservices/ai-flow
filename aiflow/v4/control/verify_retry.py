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

from pathlib import Path

from fastapi import HTTPException

from ..core.services import service_gaps
from ..core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps
from . import environment, licensing, role_health, rollout, shared_accounts, tooling
from .incidents import close_incident, open_incident
from .repositories.verify_runs import VerifyRunsRepository
from . import work_eligibility

KIND = "verify_no_verdict"
SLOW_MINUTES = 30
CONSECUTIVE = 2


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
    cooling_tickets = repository.cooling_tickets("verify")
    out = []
    for ticket in repository.verification_candidates(projects):
        if ticket["key"] in held or ticket["key"] in cooling_tickets or not can_handle(ticket):
            continue
        dev = repository.latest_dev_with_pr(ticket["key"])
        if not dev or not dev["pr_url"] or dev["account_id"] == account.id:
            continue
        if account.explicit_only and f"ai-{account.provider}" not in ticket["labels"]:
            continue
        if not allowed("verify", ticket):
            continue
        if retry_guard and not may_take(
                ctx, ticket, account.id, dev["account_id"], lambda peer, node: offers(peer, node)):
            continue
        out.append({**ticket, "step": "verify", "probation": probation("verify")})
    return out


def pool(ctx, ticket: dict, dev_account: str, offers=None, releases: Path | None = None) -> list[str]:
    """Accounts whose normal candidate path can receive this ticket, regardless of no-verdict history."""
    out = []
    for acc in ctx.repo().accounts:
        if acc.paused or acc.id == dev_account or "verify" not in role_health.account_roles(acc):
            continue
        for node in dict.fromkeys(acc.machines):
            if offers is not None:
                eligible = _offered(offers, acc.id, node, ticket["key"])
            else:
                eligible = candidate_offer(ctx, ticket, acc.id, node, releases)
            if eligible:
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


def candidate_offer(ctx, ticket: dict, account_id: str, node: str, releases: Path | None = None) -> bool:
    """Ticket-specific normal verify eligibility used by the watchdog when no API request is present."""
    repo, repository = ctx.repo(), VerifyRunsRepository(ctx.db)
    account = repo.account(account_id)
    project = repo.projects.get(str(ticket.get("project") or ""))
    node_row = repository.candidate_node(node)
    labels = set(ticket.get("labels") or [])
    if (not work_eligibility.can_take_work(ctx.db, ctx.leases, account, node)
            or account.id == repository.last_dev_account(ticket["key"])
            or "verify" not in role_health.account_roles(account)
            or (account.explicit_only and f"ai-{account.provider}" not in labels)):
        return False
    if (not project or project.paused or not node_row.get("accepting_work", True)
            or node not in shared_accounts.online_nodes(ctx.db)
            or project.key not in repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False))):
        return False
    if project.parallel_limit and repository.running_project_count(project.key) >= project.parallel_limit:
        return False
    mode = role_health.modes(ctx.db, account.id, ["verify"])["verify"]
    if mode != "on" and not (mode == "probation" and role_health.probation_ticket(ticket)):
        return False
    if ticket["key"] in set(ctx.leases.held_keys()):
        return False
    if ticket["key"] in cooling(ctx.db) or not _node_ready_for_ticket(ctx, ticket, project, node_row):
        return False
    try:
        license_state = licensing.enforce_work_entitlement(
            ctx, account.id, node, public_key=ctx.settings.license_public_key)
        licensing.enforce_project_entitlement(ctx, project.key, public_key=ctx.settings.license_public_key,
                                              state=license_state)
    except licensing.LicenseError:
        return False
    return bool(rollout.node_compatible(ctx, node, releases)["compatible"])


def _node_ready_for_ticket(ctx, ticket: dict, project, node_row: dict) -> bool:
    node_tools = node_row.get("tools") or {}
    if project.stacks or project.uses_containers:
        if toolchain_gaps(node_tools, [tool for tool in project.tools if tool != CONTAINER_RUNTIME_TOOL],
                          ctx.repo().versions, project.stacks, require_pins=bool(project.stacks)):
            return False
    if service_gaps((node_row.get("metrics") or {}).get("services"), project.required_services):
        return False
    prior = VerifyRunsRepository(ctx.db).latest_dev_with_pr(ticket["key"]) or {}
    details = prior.get("details") or {}
    repository_name = str(details.get("repository") or "")
    saved_settings = details.get("repository_settings")
    selected = (dict(saved_settings) if isinstance(saved_settings, dict) else
                project.settings_for_repo(repository_name) if repository_name else
                project.settings_for_ticket(ticket))
    if selected.get("containers") and toolchain_gaps(node_tools, [CONTAINER_RUNTIME_TOOL], ctx.repo().versions):
        return False
    if ticket["key"] in tooling.parked_tickets(ctx, node_row):
        return False
    return ticket["key"] not in environment.parked_tickets(ctx)


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
    repo = VerifyRunsRepository(ctx.db)
    already_open = set(repo.open_incident_subjects(KIND))
    open_now = set()
    for ticket in repo.tickets_in_verify():
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
    tried = []
    for account in slow["accounts"]:
        label = account["account"]
        if account["no_verdict"]:
            label += f" (no verdict x{account['no_verdict']}" + (", not offered again)" if account["barred"] else ")")
        tried.append(label)
    return f"{slow['waiting_minutes']} min · tried: {', '.join(tried) or 'no account has tried yet'}"
