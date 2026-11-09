"""Use cases for shared AI accounts and their per-machine sign-in sessions."""
from __future__ import annotations

import re
from datetime import timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from . import repository

ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
PROVIDER = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{3,63}$")


class SharedAccountRefused(Exception):
    """A shared-account operation that the route maps to its existing HTTP response."""

    def __init__(self, status: int, detail: Any):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def sync_managed_accounts(db: Any, sync_accounts: Callable[[], list[str]]) -> list[str]:
    warnings = sync_accounts()
    for row in repository.managed_shared_accounts(db):
        repository.ensure_account_row(db, row)
    return warnings


def account_or_404(ctx: Any, account_id: str):
    account = ctx.repo().account(account_id)
    if not account:
        raise SharedAccountRefused(404, f"unknown account {account_id}")
    return account


def account_view(db: Any, account: Any, *, include_signin: bool, online_nodes_fn: Callable,
                 running_by_node_fn: Callable, machines_view_fn: Callable) -> dict[str, Any]:
    status = {row["name"]: "online" if row["name"] in online_nodes_fn(db) else "offline"
              for row in repository.node_names(db)}
    running = sum(running_by_node_fn(db, account.id).values())
    return {"id": account.id, "provider": account.provider, "tier": account.tier, "roles": account.roles,
            "session_limit": account.sessions,
            "sessions_active": running,
            "machines": machines_view_fn(db, account, status, include_signin=include_signin)}


def create_account_record(ctx: Any, body: Any, user: Any, sync_accounts: Callable[[], list[str]]) -> None:
    if not ACCOUNT_ID.match(body.id) or not PROVIDER.match(body.provider):
        raise SharedAccountRefused(422, "account id and provider use lowercase letters, digits and dashes")
    if ctx.repo().account(body.id):
        raise SharedAccountRefused(409, f"account {body.id} already exists")
    repository.create_shared_account(ctx.db, body, user.subject)
    sync_accounts()


def set_session_limit_record(ctx: Any, account_id: str, body: Any, user: Any):
    account = account_or_404(ctx, account_id)
    with repository.transaction(ctx.db) as connection:
        repository.lock_account_sessions(connection, account.id)
        active = repository.running_session_count(connection, account.id)
        if body.session_limit < active:
            raise SharedAccountRefused(409, {
                "error": "limit_below_active_sessions",
                "message": f"{account.id} has {active} running sessions; wait for "
                           f"{active - body.session_limit} to finish before lowering "
                           f"the limit to {body.session_limit}.",
            })
        repository.save_session_limit(connection, account, body.session_limit, user.subject)
    return account


def enable_machine_record(ctx: Any, account_id: str, body: Any, user: Any,
                          sync_accounts: Callable[[], list[str]]):
    account = account_or_404(ctx, account_id)
    if not repository.node_exists(ctx.db, body.node):
        raise SharedAccountRefused(404, f"machine {body.node} has not joined")
    if account.runs_on(body.node):
        raise SharedAccountRefused(409, f"{account.id} is already enabled on {body.node}")
    repository.ensure_shared_account(ctx.db, account, user.subject)
    with repository.transaction(ctx.db) as connection:
        repository.lock_account_sessions(connection, account.id)
        repository.enable_machine(connection, account.id, body.node, user.subject)
    sync_accounts()
    return account


def retry_signin_record(ctx: Any, account_id: str, node: str, _user: Any):
    account = account_or_404(ctx, account_id)
    if not repository.retry_signin(ctx.db, account.id, node):
        raise SharedAccountRefused(409, f"{account.id} has no pending sign-in on {node}")
    return account


def detach_machine(ctx: Any, account: Any, node: str, actor: str) -> None:
    repository.detach_machine(ctx.db, account.id, node, actor)


def delete_account(ctx: Any, account: Any) -> bool:
    return repository.delete_managed_account(ctx.db, account.id)


def remove_machine_record(ctx: Any, account_id: str, node: str, user: Any, confirm: bool):
    account = account_or_404(ctx, account_id)
    if not account.runs_on(node):
        raise SharedAccountRefused(404, f"{account.id} is not enabled on {node}")
    if repository.managed_account_exists(ctx.db, account.id):
        raise SharedAccountRefused(409, f"{account.id} is managed from the machine account screen")
    last = len(account.machines) == 1
    if last and not confirm:
        raise SharedAccountRefused(409, {
            "error": "confirmation_required",
            "message": f"{node} is the last machine of {account.id}; removing it removes the "
                       "account from every machine. Confirm to continue.",
        })
    detach_machine(ctx, account, node, user.subject)
    removed_account = delete_account(ctx, account) if last else False
    return account, removed_account


def remove_account_record(ctx: Any, account_id: str, user: Any, confirm: bool):
    account = account_or_404(ctx, account_id)
    if repository.managed_account_exists(ctx.db, account.id):
        raise SharedAccountRefused(409, f"{account.id} is managed from the machine account screen")
    row = repository.shared_account_managed(ctx.db, account.id)
    if not confirm:
        raise SharedAccountRefused(409, {
            "error": "confirmation_required",
            "message": f"Removing {account.id} removes it from all {len(account.machines)} machines. "
                       "Confirm to continue.",
        })
    if row and row["managed"]:
        delete_account(ctx, account)
        retained = False
    else:
        for node in account.machines:
            detach_machine(ctx, account, node, user.subject)
        retained = True
    return account, retained


def signin_prompt_record(ctx: Any, account_id: str, body: Any, node: str, datetime_type: Any) -> dict:
    account = ctx.repo().account(account_id)
    if not account or not account.runs_on(node):
        raise SharedAccountRefused(403, f"account {account_id} does not run on {node}")
    parsed = urlparse(body.link)
    if parsed.scheme != "https" or not parsed.netloc or not CODE.match(body.code):
        raise SharedAccountRefused(422, "sign-in link must be https and the code a short device code")
    changed = repository.save_signin_prompt(ctx.db, account.id, node, body.link, body.code, body.expires_in_s)
    expires_at = (datetime_type.now(timezone.utc) + timedelta(seconds=body.expires_in_s)).isoformat()
    return {"stored": changed, "expires_at": expires_at}


def begin_signin_complete(ctx: Any, account_id: str, body: Any, node: str):
    account = ctx.repo().account(account_id)
    if not account or not account.runs_on(node):
        raise SharedAccountRefused(403, f"account {account_id} does not run on {node}")
    row = repository.machine_signin(ctx.db, account.id, node)
    if not row:
        raise SharedAccountRefused(409, f"{account.id} has no shared sign-in on {node}")
    attempt = row["signin_attempt"]
    if body.request_id != f"shared:{node}:{account.id}:{attempt}":
        raise SharedAccountRefused(409, "shared sign-in attempt is no longer current")
    emit_completed = False
    if row["status"] == "pending":
        changed = repository.complete_pending_signin(ctx.db, account.id, node, attempt)
        if not changed:
            current = repository.machine_signin(ctx.db, account.id, node)
            if not current or current["signin_attempt"] != attempt or current["status"] != "signed_in":
                raise SharedAccountRefused(409, "shared sign-in attempt is no longer current")
        else:
            emit_completed = True
    elif row["status"] != "signed_in":
        raise SharedAccountRefused(409, f"{account.id} is not waiting for sign-in on {node}")
    return account, emit_completed


def finish_signin_complete(ctx: Any, account_id: str) -> None:
    repository.finish_signin(ctx.db, account_id)
