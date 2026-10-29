"""Helpers every route group needs: audit, authentication, fencing (JAIF-421).

They were closures inside ``create_app``, so no route could leave ``api.py``. Here they are plain functions of the
application context; ``api.py`` delegates to them and a router in ``routes/`` reads the context from
``request.app.state.ctx`` (see the ``*_dependency`` wrappers). Behaviour, messages and status codes are unchanged.
"""
from __future__ import annotations

import hashlib
import uuid
from contextvars import ContextVar

from fastapi import Header, HTTPException, Request

from . import licensing
from .console_auth import COOKIE_NAME, ConsoleUser, resolve_session_user, role_satisfies, verify_session
from .db import J

github_connection_used: ContextVar[str] = ContextVar("github_connection_used", default="")
READ_ACTIONS = frozenset({"read", "project_catalog", "project_repositories", "project_preflight",
                          "list_import_installations", "open_installation_settings", "license_activate"})


def h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def context(request: Request):
    return request.app.state.ctx


def node_auth(ctx, authorization: str = "") -> str:
    token = authorization.removeprefix("Bearer ").strip()
    row = ctx.db.one("SELECT name FROM nodes WHERE token_hash = %s AND token_hash <> '' AND removed_at IS NULL",
                     (h(token),)) if token else None
    if not row:
        raise HTTPException(401, "unknown node token")
    return row["name"]


def with_connection(target: str, payload: dict | None) -> dict:
    """Name the GitHub connection behind a GitHub write; it is an id, never a credential."""
    payload = dict(payload or {})
    used = github_connection_used.get()
    if used and target in ("github", "aiflow-control") and "connection" not in payload:
        payload["connection"] = used
    return payload


def audit_in(connection, target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str,
             accepted: bool, result: str, payload: dict | None = None) -> None:
    connection.execute(
        "INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (target, action, ticket, run_id, fence, actor, accepted, result[:500], J(with_connection(target, payload))))


def audit(ctx, target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str, accepted: bool,
          result: str, payload: dict | None = None) -> None:
    with ctx.db.transaction() as connection:
        audit_in(connection, target, action, ticket, run_id, fence, actor, accepted, result, payload)


def console_user(ctx, request: Request, permissions: set[str] | str | None, target: str = "console",
                 action: str = "read", node: str = "") -> ConsoleUser:
    """Require a signed, unexpired console session. Role and subject headers are deliberately ignored."""
    secret = ctx.settings.console_session_secret
    if len(secret.encode()) < 32:
        if action != "read":
            audit(ctx, target, action, node, "", None, "", False, "console session signing key is not configured")
        raise HTTPException(503, "console session authentication is not configured")
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), secret)
    user = resolve_session_user(ctx.db, claims) if claims else None
    if not user:
        if action != "read":
            audit(ctx, target, action, node, "", None, "", False, "authenticated console session required")
        raise HTTPException(401, "authenticated console session required")
    required = {permissions} if isinstance(permissions, str) else permissions
    if required is not None and not role_satisfies(user.effective_roles, required):
        if action != "read":
            audit(ctx, target, action, node, "", None, user.subject, False, "not available for your role")
        raise HTTPException(403, "not available for your role")
    if action not in READ_ACTIONS:
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
    return user


def optional_console_user(ctx, request: Request) -> ConsoleUser | None:
    """Read a session when present without changing the legacy open-console mode."""
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
    return resolve_session_user(ctx.db, claims) if claims else None


def run_row(ctx, run_id: str) -> dict:
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(404, "unknown run")
    row = ctx.db.one("SELECT * FROM runs WHERE id = %s", (run_id,))
    if not row:
        raise HTTPException(404, "unknown run")
    return row


def fenced(ctx, run_id: str, fence: int, target: str, action: str, payload: dict | None = None) -> dict:
    run = run_row(ctx, run_id)
    ok = int(run["fence"]) == int(fence) and ctx.leases.valid(run["ticket_key"], str(run["id"]), int(fence))
    if not ok:
        audit(ctx, target, action, run["ticket_key"], run_id, fence, run["account_id"], False, "stale fencing token", payload)
        ctx.events.emit("write.refused", ticket=run["ticket_key"], run_id=run_id, fence=fence,
                        account=run["account_id"], node=run["node"], reason=f"{target}.{action}: stale fence")
        raise HTTPException(409, "fencing token is no longer valid")
    if run["step"] == "master" and target != "master":
        audit(ctx, target, action, run["ticket_key"], run_id, fence, run["account_id"], False,
              "master writes must use the restricted master action API", payload)
        ctx.events.emit("write.refused", ticket=run["ticket_key"], run_id=run_id, fence=fence,
                        account=run["account_id"], node=run["node"],
                        reason=f"{target}.{action}: master writes require the restricted action API")
        raise HTTPException(403, "master writes must use the restricted master action API")
    return run


# FastAPI dependencies for routers outside api.py: the context comes from the app, never from a closure.
def node_auth_dependency(request: Request, authorization: str = Header(default="")) -> str:
    return node_auth(context(request), authorization)


def console_user_for(request: Request, permissions: set[str] | str | None, target: str = "console",
                     action: str = "read", node: str = "") -> ConsoleUser:
    return console_user(context(request), request, permissions, target, action, node)
