"""Shared account and machine gates used by normal candidate selection and watchdog probes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import shared_accounts
from .repositories.accounts import AccountsRepository


@dataclass(frozen=True)
class CandidateDecision:
    eligible: bool
    rejected_by: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CandidateRule:
    name: str
    reject: Callable[[Any, dict, dict], str | None]


_CANDIDATE_RULES: dict[str, CandidateRule] = {}


def register_candidate_rule(name: str, reject: Callable[[Any, dict, dict], str | None]) -> None:
    """Register one named work-eligibility rule at the control-service registration point."""
    if not name or name in _CANDIDATE_RULES:
        raise ValueError(f"candidate eligibility rule is empty or already registered: {name!r}")
    _CANDIDATE_RULES[name] = CandidateRule(name, reject)


def evaluate_candidate(state: Any, ticket: dict, rules: tuple[str, ...], facts: dict | None = None
                       ) -> CandidateDecision:
    """Run named eligibility rules in order and retain the first rejection explanation."""
    context = facts or {}
    for name in rules:
        rule = _CANDIDATE_RULES[name]
        reason = rule.reject(state, ticket, context)
        if reason:
            return CandidateDecision(False, rule.name, reason)
    return CandidateDecision(True)


def master_paused(db, account_id: str) -> bool:
    """Whether the master has an active temporary pause for this account."""
    return AccountsRepository(db).master_paused(account_id)


def can_take_work(db, leases, account, node: str) -> bool:
    """The common account-level gates applied before the normal role-specific candidate rules."""
    return bool(account and account.runs_on(node) and not account.paused
                and not master_paused(db, account.id) and not leases.cooling(account.id)
                and shared_accounts.may_take(db, account, node)[0])
