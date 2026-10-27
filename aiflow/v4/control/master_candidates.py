"""Eligibility for the global master monitoring cycle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import role_health, shared_accounts
from .repositories.accounts import AccountsRepository
from .repositories.runs import RunsRepository

MASTER_SCOPE = "master:global"
MASTER_CYCLE_S = 60


class MasterAccountUnavailable(ValueError):
    """The requested account is not assigned to the authenticated node."""


def candidate(ctx, account_id: str, node: str, runtime, account_paused, budget_state) -> dict | None:
    repo = ctx.repo()
    account = repo.account(account_id)
    if not account or not account.runs_on(node):
        raise MasterAccountUnavailable(f"account {account_id} does not run on {node}")
    if not _account_ready(ctx, account, node, account_paused):
        return None
    if not _master_role_available(ctx, account_id, runtime):
        return None
    if not _can_take_and_within_budget(ctx, account, node, budget_state):
        return None
    if _scope_is_claimed(ctx):
        return None
    recent = RunsRepository(ctx.db).latest_master_cycle()
    failover = _should_fail_over(ctx, repo, account, recent, account_paused)
    if failover is None:
        return None
    if not failover and MASTER_SCOPE in RunsRepository(ctx.db).cooling_tickets("master"):
        return None
    if _cycle_recently_finished(recent):
        return None
    return {"key": MASTER_SCOPE, "project": "", "status": "Master", "type": "Master", "priority": "Highest",
            "labels": [], "summary": "Master monitoring cycle",
            "description": "Review incidents and follow up on blocked or slow work.",
            "jira_created": None, "step": "master"}


def _master_role_available(ctx, account_id: str, runtime) -> bool:
    if runtime.account_quota_at_risk(account_id, {"master"}):
        return False
    return (account_id, "master") not in role_health.disabled_roles(ctx.db)


def _can_take_and_within_budget(ctx, account, node: str, budget_state) -> bool:
    if not shared_accounts.may_take(ctx.db, account, node)[0]:
        return False
    return bool(budget_state()["enabled"])


def _scope_is_claimed(ctx) -> bool:
    return MASTER_SCOPE in set(ctx.leases.held_keys())


def _account_ready(ctx, account, node: str, account_paused) -> bool:
    if account.paused or "master" not in (account.roles or []):
        return False
    if ctx.leases.cooling(account.id) or account_paused(account.id):
        return False
    ready = AccountsRepository(ctx.db).master_readiness(account.id, node)
    if not ready or ready["status"] not in ("idle", "working", "probing"):
        return False
    return ready["node_status"] == "online"


def _should_fail_over(ctx, repo, account, recent, account_paused) -> bool | None:
    if not recent:
        return False
    lease_lost = (recent["status"] == "running"
                  and ctx.leases.master_holder() != f"{recent['id']}:{recent['fence']}")
    failed_over = recent["outcome"] in ("quota", "lost") or lease_lost
    if _other_provider_is_ready(repo, account, recent, failed_over, account_paused, ctx):
        return None
    return failed_over


def _other_provider_is_ready(repo, account, recent, failed_over: bool, account_paused, ctx) -> bool:
    if not failed_over or recent["provider"] != account.provider:
        return False
    failed_at = recent["ended_at"] or datetime.now(timezone.utc)
    if failed_at <= datetime.now(timezone.utc) - timedelta(seconds=MASTER_CYCLE_S):
        return False
    return any(other.provider != account.provider and "master" in (other.roles or [])
               and any(_account_ready(ctx, other, machine, account_paused) for machine in other.machines)
               for other in repo.accounts)


def _cycle_recently_finished(recent) -> bool:
    return bool(recent and recent["ended_at"] and recent["ended_at"] > datetime.now(timezone.utc)
                - timedelta(seconds=MASTER_CYCLE_S)
                and recent["outcome"] == "done")
