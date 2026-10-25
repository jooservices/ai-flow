"""Shared account and machine gates used by normal candidate selection and watchdog probes."""
from __future__ import annotations

from . import shared_accounts
from .repositories.accounts import AccountsRepository


def master_paused(db, account_id: str) -> bool:
    """Whether the master has an active temporary pause for this account."""
    return AccountsRepository(db).master_paused(account_id)


def can_take_work(db, leases, account, node: str) -> bool:
    """The common account-level gates applied before the normal role-specific candidate rules."""
    return bool(account and account.runs_on(node) and not account.paused
                and not master_paused(db, account.id) and not leases.cooling(account.id)
                and shared_accounts.may_take(db, account, node)[0])
