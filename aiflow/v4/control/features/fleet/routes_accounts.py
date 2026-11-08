"""Route installer groups for fleet.
"""
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from .bodies import ConfirmBody, ManagedAccountCreateBody, ManagedAccountSignInStatusBody, ManagedAccountUpdateBody
from .events import AiAccountAdded, AiAccountManagementChanged, AiAccountRemoved, AiAccountSignInCompleted, AiAccountSignInRestarted
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException, Request
from ... import licensing, load_policy_api, quota_refresh


SIGN_IN_LINK_LIFETIME = timedelta(minutes=15)


def _sign_in_link_expired(expires_at) -> bool:
    """A provider link is single-use for its window: once its expiry passes, only a fresh sign-in can replace it."""
    return expires_at is not None and expires_at <= datetime.now(UTC)


def _account_update_sessions(body, account) -> int:
    return body.sessions if body.sessions is not None else max(1, int(account.parallel or 1))


def _account_update_roles(body, account, job_roles) -> list[str]:
    if body.jobs is not None:
        return [job_roles[job] for job in dict.fromkeys(body.jobs)]
    return list(account.roles or [])


def _check_account_session_capacity(audit_in, connection, account_id, actor, total, account, sessions) -> None:
    if total - max(1, int(account.parallel or 1)) + sessions > 8:
        audit_in(connection, "ai-account", "update", account_id, "", None, actor, False,
                 "machine session limit is eight", {"current_sessions": total, "requested_sessions": sessions})
        raise HTTPException(422, "a machine can run at most 8 sessions")


def _validate_sign_in_status(body) -> None:
    if body.sign_in_url:
        try:
            parsed = urlsplit(body.sign_in_url)
            _ = parsed.port
        except ValueError as exc:
            raise HTTPException(422, "provider sign-in URL is invalid") from exc
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(422, "provider sign-in URL must be an HTTPS URL")
    if body.code and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,79}", body.code):
        raise HTTPException(422, "provider sign-in code has an invalid format")
    if body.status == "waiting" and not body.sign_in_url:
        raise HTTPException(422, "provider sign-in must supply its authorization URL")


def _register_add_managed_account(app, ctx, deps, support=None):
    audit_in = deps.audit_in
    console_user = deps.console_user
    require_capacity = deps.require_capacity
    account_jobs = support.account_jobs
    job_roles = support.JOB_ROLES
    lock_machine_capacity = support.lock_machine_capacity
    machine_capacity = support.machine_capacity
    @app.post("/v1/machines/{name}/accounts")
    def add_managed_account(name: str, body: ManagedAccountCreateBody, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="add", node=name)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise HTTPException(422, "invalid machine name")
        roles = [job_roles[job] for job in dict.fromkeys(body.jobs)]
        request_id = str(uuid.uuid4())
        with ctx.db.transaction() as connection:
            lock_machine_capacity(connection, name)
            licensing.lock_resource_admission(connection)
            node = connection.execute("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,)).fetchone()
            if not node:
                audit_in(connection, "ai-account", "add", name, "", None, user.subject, False,
                         "machine is not registered", {})
                raise HTTPException(404, "machine is not registered")
            repo = ctx.repo()
            capacity = machine_capacity(repo, name)
            if capacity + body.sessions > 8:
                audit_in(connection, "ai-account", "add", name, "", None, user.subject, False,
                         "machine session limit is eight",
                         {"current_sessions": capacity, "requested_sessions": body.sessions})
                raise HTTPException(422, "a machine can run at most 8 sessions")
            require_capacity("ai_accounts", connection=connection)
            existing_ids = {account.id for account in repo.accounts}
            existing_ids.update(row["id"] for row in connection.execute(
                "SELECT id FROM managed_ai_accounts").fetchall())
            index = 1
            while f"{body.provider}-{name}-{index:02d}" in existing_ids:
                index += 1
            account_id = f"{body.provider}-{name}-{index:02d}"
            connection.execute("INSERT INTO managed_ai_accounts (id,name,provider,node,tier,roles,parallel,paused,"
                               "sign_in_request_id,sign_in_status,sign_in_started_at)"
                               " VALUES (%s,%s,%s,%s,'middle',%s,%s,false,%s,'queued',now())",
                               (account_id, body.name.strip(), body.provider, name, roles, body.sessions, request_id))
            connection.execute("INSERT INTO accounts (id,provider,node,tier,roles,status)"
                               " VALUES (%s,%s,%s,'middle',%s,'signed_out')",
                               (account_id, body.provider, name, roles))
            fleet_routes.publish_for(ctx, AiAccountAdded(account=account_id, actor=user.subject, node=name, provider=body.provider,
                                            name=body.name.strip(), sessions=body.sessions,
                                            jobs=account_jobs(roles)))
        ctx.events.emit("account.added", account=account_id, provider=body.provider, node=name,
                        outcome="sign_in_queued", reason="provider sign-in started on the machine")
        return {"account": account_id, "name": body.name.strip(), "node": name, "provider": body.provider,
                "sessions": body.sessions, "jobs": account_jobs(roles), "status": "queued", "sign_in_url": "", "code": ""}
    return add_managed_account

def _register_update_managed_account(app, ctx, deps, support=None):
    audit_in = deps.audit_in
    console_user = deps.console_user
    account_jobs = support.account_jobs
    lock_machine_capacity = support.lock_machine_capacity
    machine_capacity = support.machine_capacity
    save_managed_account = support.save_managed_account
    job_roles = support.JOB_ROLES
    @app.patch("/v1/accounts/{account_id}")
    def update_managed_account(account_id: str, body: ManagedAccountUpdateBody, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="update", node=account_id)
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        if body.sessions is None and body.jobs is None and body.action is None:
            raise HTTPException(422, "choose sessions, jobs, pause, or resume")
        with ctx.db.transaction() as connection:
            lock_machine_capacity(connection, account.node)
            repo = ctx.repo()
            account = repo.account(account_id)
            if not account:
                raise HTTPException(404, "AI account is not registered")
            sessions = _account_update_sessions(body, account)
            total = machine_capacity(repo, account.node)
            _check_account_session_capacity(audit_in, connection, account_id, user.subject, total, account, sessions)
            roles = _account_update_roles(body, account, job_roles)
            paused = body.action == "pause" if body.action else account.paused
            save_managed_account(account, roles, sessions, paused, connection=connection)
            connection.execute("UPDATE accounts SET roles=%s,updated_at=now() WHERE id=%s", (roles, account_id))
            connection.execute("UPDATE shared_accounts SET roles=%s WHERE id=%s", (roles, account_id))
            action = body.action or "update"
            payload = {"sessions": sessions, "jobs": account_jobs(roles), "paused": paused}
            fleet_routes.publish_for(ctx, AiAccountManagementChanged(account=account_id, actor=user.subject, action=action,
                                                        sessions=sessions, jobs=payload["jobs"], paused=paused))
        ctx.events.emit("account.management_changed", account=account_id, node=account.node,
                        outcome=action, reason="account settings updated", data=payload)
        return {"account": account_id, **payload}
    return update_managed_account

def _register_restart_account_sign_in(app, ctx, deps, support=None):
    console_user = deps.console_user
    save_managed_account = support.save_managed_account
    @app.post("/v1/accounts/{account_id}/sign-in")
    def restart_account_sign_in(account_id: str, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="sign_in_again", node=account_id)
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        if ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1", (account_id,)):
            raise HTTPException(409, "account has an active session; wait for it to finish before signing in again")
        request_id = str(uuid.uuid4())
        with ctx.db.transaction() as connection:
            save_managed_account(account, list(account.roles or []), max(1, int(account.parallel or 1)),
                                 account.paused, connection=connection)
            connection.execute("UPDATE managed_ai_accounts SET sign_in_request_id=%s,sign_in_status='queued',"
                               "sign_in_url='',sign_in_code='',sign_in_expires_at=NULL,sign_in_error='',"
                               "sign_in_started_at=now(),updated_at=now() WHERE id=%s",
                               (request_id, account_id))
            connection.execute("UPDATE accounts SET status='signed_out',last_error='',updated_at=now() WHERE id=%s",
                               (account_id,))
            fleet_routes.publish_for(ctx, AiAccountSignInRestarted(account=account_id, actor=user.subject,
                                                      provider=account.provider, node=account.node))
        ctx.events.emit("account.sign_in_started", account=account_id, provider=account.provider,
                        node=account.node, outcome="queued", reason="owner requested provider sign-in again")
        return {"account": account_id, "status": "queued", "sign_in_url": "", "code": ""}
    return restart_account_sign_in

def _register_account_spec(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    @app.get("/v1/accounts/{account_id}/spec")
    def account_spec(account_id: str, node: str = Depends(node_auth)) -> dict:
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or not acc.runs_on(node):
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        model, effort = repo.model_for(acc.provider, acc.tier)
        return {"account": acc.id, "provider": acc.provider, "tier": acc.tier, "model": model, "effort": effort}
    return account_spec

def _register_account_sign_in_details(app, ctx, deps, support=None):
    console_user = deps.console_user
    @app.get("/v1/accounts/{account_id}/sign-in")
    def account_sign_in_details(account_id: str, request: Request) -> dict:
        console_user(request, {"nodes.manage"})
        row = ctx.db.one("SELECT node,provider,sign_in_status,sign_in_url,sign_in_code,sign_in_error,"
                         "sign_in_expires_at FROM managed_ai_accounts"
                         " WHERE id=%s AND removed_at IS NULL AND sign_in_status<>'idle'", (account_id,))
        if not row:
            raise HTTPException(404, "no active sign-in link for this account")
        if _sign_in_link_expired(row["sign_in_expires_at"]):
            raise HTTPException(410, "provider sign-in link has expired; start a new sign-in")
        return {"account": account_id, "node": row["node"], "provider": row["provider"],
                "status": row["sign_in_status"], "sign_in_url": row["sign_in_url"],
                "code": row["sign_in_code"], "error": row["sign_in_error"],
                "expires_at": row["sign_in_expires_at"].isoformat() if row["sign_in_expires_at"] else None}
    return account_sign_in_details

def _register_report_account_sign_in_status(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    @app.post("/v1/accounts/{account_id}/sign-in/status")
    def report_account_sign_in_status(account_id: str, body: ManagedAccountSignInStatusBody,
                                      node: str = Depends(node_auth)) -> dict:
        row = ctx.db.one("SELECT node,sign_in_request_id,sign_in_status,sign_in_expires_at FROM managed_ai_accounts"
                         " WHERE id=%s AND removed_at IS NULL", (account_id,))
        if not row or row["node"] != node:
            raise HTTPException(404, "account sign-in request is not assigned to this machine")
        if row["sign_in_request_id"] != body.request_id:
            raise HTTPException(409, "sign-in request has been replaced")
        if body.status == "waiting" and _sign_in_link_expired(row["sign_in_expires_at"]):
            raise HTTPException(409, "provider sign-in link has expired; start a new sign-in")
        _validate_sign_in_status(body)
        with ctx.db.pool.connection() as connection:
            updated = connection.execute(
                "UPDATE managed_ai_accounts SET sign_in_status=%s,"
                "sign_in_url=CASE WHEN %s<>'' THEN %s ELSE sign_in_url END,"
                "sign_in_code=CASE WHEN %s<>'' THEN %s ELSE sign_in_code END,"
                "sign_in_expires_at=CASE WHEN %s<>'' AND %s<>sign_in_url THEN now()+%s ELSE sign_in_expires_at END,"
                "sign_in_error=%s,updated_at=now() WHERE id=%s AND node=%s AND sign_in_request_id=%s"
                " AND removed_at IS NULL RETURNING id",
                (body.status, body.sign_in_url, body.sign_in_url, body.code, body.code,
                 body.sign_in_url, body.sign_in_url, SIGN_IN_LINK_LIFETIME,
                 body.error[:300], account_id, node, body.request_id)).fetchone()
            if not updated:
                raise HTTPException(409, "sign-in request has been replaced")
            if body.status == "completed":
                quota_refresh.on_sign_in_completed(connection, account_id)
        return {"account": account_id, "status": body.status}
    return report_account_sign_in_status

def _register_complete_account_sign_in(app, ctx, deps, support=None):
    console_user = deps.console_user
    @app.post("/v1/accounts/{account_id}/sign-in/complete")
    def complete_account_sign_in(account_id: str, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="sign_in_complete",
                            node=account_id)
        with ctx.db.transaction() as connection:
            row = connection.execute(
                "SELECT m.node,m.provider,m.sign_in_status,a.status,a.last_probe_at,m.sign_in_started_at"
                " FROM managed_ai_accounts m JOIN accounts a ON a.id=m.id"
                " WHERE m.id=%s AND m.removed_at IS NULL AND m.sign_in_status<>'idle'"
                " FOR UPDATE OF m", (account_id,)).fetchone()
            if not row:
                raise HTTPException(404, "no pending sign-in for this account")
            if row["sign_in_status"] != "completed":
                raise HTTPException(409, "provider authorization has not completed on the machine")
            if row["status"] not in ("idle", "working") or not row["last_probe_at"] or not row["sign_in_started_at"] \
                    or row["last_probe_at"] < row["sign_in_started_at"]:
                raise HTTPException(409, "the machine has not reported a successful account check since sign-in started")
            connection.execute("UPDATE managed_ai_accounts SET sign_in_url='',sign_in_code='',sign_in_error='',"
                               "sign_in_status='idle',sign_in_request_id='',sign_in_expires_at=NULL,"
                               "sign_in_started_at=NULL,updated_at=now() WHERE id=%s",
                               (account_id,))
            fleet_routes.publish_for(ctx, AiAccountSignInCompleted(account=account_id, actor=user.subject, node=row["node"],
                                                      provider=row["provider"]))
        return {"account": account_id, "signed_in": True}
    return complete_account_sign_in

def _register_remove_managed_account(app, ctx, deps, support=None):
    audit = deps.audit
    console_user = deps.console_user
    @app.delete("/v1/accounts/{account_id}")
    def remove_managed_account(account_id: str, body: ConfirmBody, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="remove", node=account_id)
        if not body.confirm:
            fleet_routes.audit_refused_write(audit, "ai-account", "remove", account_id, user.subject,
                                              "confirmation required")
            raise HTTPException(400, "confirm account removal")
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        active = ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id=%s AND status='running'", (account_id,))
        if active and active["n"]:
            raise HTTPException(409, "account has an active session; wait for it to finish before removing it")
        with ctx.db.transaction() as connection:
            connection.execute("INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,removed_at)"
                               " VALUES (%s,%s,%s,%s,%s,now()) ON CONFLICT (id) DO UPDATE SET removed_at=now(),"
                               "updated_at=now()", (account.id, account.provider, account.node, account.tier,
                                                     account.roles))
            connection.execute("DELETE FROM accounts WHERE id=%s", (account_id,))
            fleet_routes.publish_for(ctx, AiAccountRemoved(account=account_id, actor=user.subject, node=account.node))
        ctx.events.emit("account.removed", account=account_id, provider=account.provider, node=account.node,
                        outcome="removed", reason="removed from Machines and AI screen")
        return {"account": account_id, "removed": True}
    return remove_managed_account

def install_managed_account_routes(app, ctx, deps, helpers):
    support = SimpleNamespace(
        JOB_ROLES=helpers.JOB_ROLES,
        account_jobs=helpers.account_jobs,
        lock_machine_capacity=helpers.lock_machine_capacity,
        machine_capacity=helpers.machine_capacity,
        save_managed_account=helpers.save_managed_account,
    )
    add = _register_add_managed_account(app, ctx, deps, support)
    update = _register_update_managed_account(app, ctx, deps, support)
    restart = _register_restart_account_sign_in(app, ctx, deps, support)
    spec = _register_account_spec(app, ctx, deps, support)
    details = _register_account_sign_in_details(app, ctx, deps, support)
    report = _register_report_account_sign_in_status(app, ctx, deps, support)
    complete = _register_complete_account_sign_in(app, ctx, deps, support)
    remove = _register_remove_managed_account(app, ctx, deps, support)
    load_policy_api.register_machine_load_limits(app, ctx, deps.console_user, deps.audit)
    return SimpleNamespace(JOB_ROLES=support.JOB_ROLES, account_jobs=support.account_jobs,
                           lock_machine_capacity=support.lock_machine_capacity, machine_capacity=support.machine_capacity,
                           save_managed_account=support.save_managed_account, add_managed_account=add,
                           update_managed_account=update, restart_account_sign_in=restart, account_spec=spec,
                           account_sign_in_details=details, report_account_sign_in_status=report,
                           complete_account_sign_in=complete, remove_managed_account=remove)
