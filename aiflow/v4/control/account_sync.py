"""Synchronize configured accounts and machines within the active licence limits."""
from __future__ import annotations

import logging

from . import licensing
from .repositories.accounts import AccountsRepository
from .repositories.nodes import NodesRepository

log = logging.getLogger("aiflow4.account_sync")


def sync_configured(ctx, public_key: str) -> list[str]:
    configured = ctx._configured_repo()
    accounts = AccountsRepository(ctx.db)
    nodes = NodesRepository(ctx.db)
    removed_accounts = accounts.removed_managed_ids()
    removed_nodes = nodes.removed_names()
    warnings = []
    for account in configured.accounts:
        if account.id not in removed_accounts and account.node not in removed_nodes:
            _sync_account(ctx, accounts, account, public_key, warnings)
    for node in configured.nodes.values():
        _sync_node(ctx, nodes, node, public_key, warnings)
    return warnings


def _sync_account(ctx, accounts: AccountsRepository, account, public_key: str, warnings: list[str]) -> None:
    if not accounts.configured_account_exists(account.id):
        try:
            # Startup admits configured accounts in order, matching the licence usage count.
            licensing.require_capacity(ctx, "ai_accounts", count=accounts.registered_count(), public_key=public_key)
        except licensing.LicenseError as exc:
            _record_warning(warnings, f"Configured AI account {account.id} was not synced: {exc}")
            return
    accounts.sync_configured(account)


def _sync_node(ctx, nodes: NodesRepository, node, public_key: str, warnings: list[str]) -> None:
    removed = nodes.configured_node_removed(node.name)
    if removed:
        return
    if removed is None:
        try:
            licensing.require_capacity(ctx, "machines", public_key=public_key)
        except licensing.LicenseError as exc:
            _record_warning(warnings, f"Configured machine {node.name} was not synced: {exc}")
            return
    # Keep the IP reported at join; an existing node update preserves its live address.
    nodes.sync_configured(node)


def _record_warning(warnings: list[str], message: str) -> None:
    warnings.append(message)
    log.warning("%s", message)
