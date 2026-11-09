"""HTTP routes for shared accounts and their per-machine sessions."""
from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import Depends, HTTPException, Request

from . import shared_sessions_service as service
from .bodies import AccountBody, LimitBody, MachineBody, SigninCompleteBody, SigninPromptBody
from .events import (SharedAccountCreated, SharedAccountMachineEnabled, SharedAccountMachineRemoved,
                     SharedAccountRemoved, SharedAccountSessionLimitSet, SharedAccountSignInRetried)


def _account_view(ctx: Any, account: Any, include_signin: bool, helpers: Any) -> dict:
    return service.account_view(ctx.db, account, include_signin=include_signin,
                                online_nodes_fn=helpers.online_nodes,
                                running_by_node_fn=helpers.running_by_node,
                                machines_view_fn=helpers.machines_view)


def _account_or_404(ctx: Any, account_id: str):
    try:
        return service.account_or_404(ctx, account_id)
    except service.SharedAccountRefused as refused:
        raise HTTPException(refused.status, refused.detail) from refused


def _register_listing_and_creation(app: Any, ctx: Any, console_user: Any, sync_accounts: Any,
                                   helpers: Any) -> None:
    @app.get("/v1/shared-accounts")
    def list_accounts(request: Request) -> dict:
        user = console_user(request, {"console.read"})
        include_signin = "nodes.manage" in user.permissions or "*" in user.permissions
        return {"accounts": [_account_view(ctx, account, include_signin, helpers)
                              for account in ctx.repo().accounts]}

    @app.post("/v1/shared-accounts", status_code=201)
    def create_account(body: AccountBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="create", node=body.id)
        try:
            service.create_account_record(ctx, body, user, sync_accounts)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused
        helpers.publish_for(ctx, SharedAccountCreated(account=body.id, actor=user.subject))
        account = _account_or_404(ctx, body.id)
        return _account_view(ctx, account, True, helpers)


def _register_session_limit(app: Any, ctx: Any, console_user: Any, helpers: Any) -> None:
    @app.put("/v1/shared-accounts/{account_id}/session-limit")
    def set_limit(account_id: str, body: LimitBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="session_limit", node=account_id)
        try:
            account = service.set_session_limit_record(ctx, account_id, body, user)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused
        helpers.publish_for(ctx, SharedAccountSessionLimitSet(
            account=account.id, actor=user.subject, session_limit=body.session_limit))
        account = _account_or_404(ctx, account_id)
        return _account_view(ctx, account, True, helpers)


def _register_machine_enable_and_retry(app: Any, ctx: Any, console_user: Any, sync_accounts: Any,
                                       helpers: Any) -> None:
    @app.post("/v1/shared-accounts/{account_id}/machines", status_code=201)
    def enable_machine(account_id: str, body: MachineBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="enable_machine", node=account_id)
        try:
            account = service.enable_machine_record(ctx, account_id, body, user, sync_accounts)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused
        helpers.publish_for(ctx, SharedAccountMachineEnabled(account=account.id, actor=user.subject, node=body.node))
        ctx.events.emit("account.machine_enabled", account=account.id, node=body.node, outcome="pending")
        account = _account_or_404(ctx, account_id)
        return _account_view(ctx, account, True, helpers)

    @app.post("/v1/shared-accounts/{account_id}/machines/{node}/signin")
    def retry_signin(account_id: str, node: str, request: Request) -> dict:
        """Start a fresh sign-in attempt on one machine with a new request id."""
        user = console_user(request, {"nodes.manage"}, target="account", action="retry_signin", node=account_id)
        try:
            account = service.retry_signin_record(ctx, account_id, node, user)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused
        helpers.publish_for(ctx, SharedAccountSignInRetried(account=account.id, actor=user.subject, node=node))
        account = _account_or_404(ctx, account_id)
        return _account_view(ctx, account, True, helpers)


def _register_machine_removal(app: Any, ctx: Any, console_user: Any, helpers: Any) -> None:
    @app.delete("/v1/shared-accounts/{account_id}/machines/{node}")
    def remove_machine(account_id: str, node: str, request: Request, confirm: bool = False) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="remove_machine", node=account_id)
        try:
            account, removed_account = service.remove_machine_record(ctx, account_id, node, user, confirm)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused
        helpers.publish_for(ctx, SharedAccountMachineRemoved(account=account.id, actor=user.subject, node=node,
                                                             account_removed=removed_account))
        ctx.events.emit("account.machine_removed", account=account.id, node=node, outcome="removed")
        machines = [] if removed_account else [name for name in account.machines if name != node]
        return {"account": account.id, "node": node, "account_removed": removed_account, "machines": machines}


def _register_account_removal(app: Any, ctx: Any, console_user: Any, helpers: Any) -> None:
    @app.delete("/v1/shared-accounts/{account_id}")
    def remove_account(account_id: str, request: Request, confirm: bool = False) -> dict:
        user = console_user(request, {"nodes.manage"}, target="account", action="remove", node=account_id)
        try:
            account, retained = service.remove_account_record(ctx, account_id, user, confirm)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused
        helpers.publish_for(ctx, SharedAccountRemoved(account=account.id, actor=user.subject, source_retained=retained))
        ctx.events.emit("account.removed", account=account.id, outcome="removed")
        return {"account": account.id, "removed": True, "source_retained": retained,
                "machines": list(account.machines)}


def _register_node_signin(app: Any, ctx: Any, node_auth: Any, helpers: Any) -> None:
    @app.post("/v1/accounts/{account_id}/signin-prompt")
    def signin_prompt(account_id: str, body: SigninPromptBody, node: str = Depends(node_auth)) -> dict:
        """A machine reports a short-lived provider sign-in link and device code to Command Center."""
        try:
            return service.signin_prompt_record(ctx, account_id, body, node, helpers.datetime)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused

    @app.post("/v1/accounts/{account_id}/signin-complete")
    def signin_complete(account_id: str, body: SigninCompleteBody, node: str = Depends(node_auth)) -> dict:
        """A successful provider login unlocks the local worker's credential probe for this machine."""
        try:
            account, emit_completed = service.begin_signin_complete(ctx, account_id, body, node)
        except service.SharedAccountRefused as refused:
            raise HTTPException(refused.status, refused.detail) from refused
        if emit_completed:
            ctx.events.emit("account.machine_signed_in", account=account.id, node=node, outcome="completed")
        service.finish_signin_complete(ctx, account.id)
        return {"completed": True, "account": account.id, "node": node}


def install_shared_session_routes(app: Any, ctx: Any, console_user: Any, node_auth: Any, helpers: Any) -> None:
    sync_accounts = partial(service.sync_managed_accounts, ctx.db, app.state.sync_accounts)
    app.state.sync_accounts = sync_accounts
    _register_listing_and_creation(app, ctx, console_user, sync_accounts, helpers)
    _register_session_limit(app, ctx, console_user, helpers)
    _register_machine_enable_and_retry(app, ctx, console_user, sync_accounts, helpers)
    _register_machine_removal(app, ctx, console_user, helpers)
    _register_account_removal(app, ctx, console_user, helpers)
    _register_node_signin(app, ctx, node_auth, helpers)
