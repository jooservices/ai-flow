"""The security screen's two-factor routes (any signed-in person, for their own account)."""
from __future__ import annotations

import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import audit as audit_log
from . import mfa
from .console_auth import ConsoleUser, has_permission, issue_session
from .login import mfa_role, require_mfa, session_ttl_s
from .security_guard import SecurityKit


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)


def own_row(kit: SecurityKit, user: ConsoleUser) -> dict:
    row = kit.ctx.db.one("SELECT email, role, roles, totp_secret, totp_enabled FROM console_users WHERE email=%s",
                         (user.subject,))
    if not row:
        raise HTTPException(404, "unknown person")
    return row


def verified(kit: SecurityKit, user: ConsoleUser, row: dict, code: str) -> str:
    factor = mfa.check_second_factor(kit.ctx.db, row, user.subject, code, kit.secret())
    if not factor:
        raise HTTPException(403, "that code is not valid")
    return factor


def mfa_status_view(kit: SecurityKit, user: ConsoleUser) -> dict:
    row = own_row(kit, user)
    return {"enabled": bool(row["totp_enabled"]), "recovery_codes_left": mfa.recovery_codes_left(kit.ctx.db, user.subject),
            "required": require_mfa(kit.ctx) and mfa_role(row.get("roles") or row["role"]),
            "setup_required": user.mfa_pending,
            "can_manage_security": has_permission(user.effective_roles, "security.manage")}


def enable_response(kit: SecurityKit, user: ConsoleUser, codes: list[str]) -> JSONResponse:
    response = JSONResponse({"enabled": True, "recovery_codes": codes}, headers={"Cache-Control": "no-store"})
    if user.mfa_pending:  # the limited session becomes a full one on the same session row
        ttl = session_ttl_s(kit.ctx)
        token = issue_session(user.subject, user.assigned_roles, int(time.time()) + ttl, kit.secret(), sid=user.sid)
        kit.app.state.set_session_cookies(response, token, ttl)
    return response


def enable_two_factor(kit: SecurityKit, request: Request, user: ConsoleUser, code: str) -> JSONResponse:
    row = own_row(kit, user)
    if row["totp_enabled"] or not row["totp_secret"]:
        raise HTTPException(409, "start the setup first")
    step = mfa.match_step(mfa.read_secret(row, user.subject, kit.secret()), code)
    if step is None:
        raise HTTPException(403, "that code is not valid; check the time on your phone and try again")
    kit.ctx.db.run("UPDATE console_users SET totp_enabled=true, totp_last_step=%s, updated_at=now() WHERE email=%s",
                   (step, user.subject))
    codes = mfa.replace_recovery_codes(kit.ctx.db, user.subject, kit.secret())
    audit_log.record(kit.ctx.db, user.subject, "security", "mfa_enabled", user.subject, {}, kit.ip_of(request))
    return enable_response(kit, user, codes)


def install_security_mfa(kit: SecurityKit) -> None:
    app = kit.app

    @app.get("/v1/security/mfa")
    def mfa_status(request: Request) -> dict:
        user = kit.require(request, allow_pending=True)
        return mfa_status_view(kit, user)

    @app.post("/v1/security/mfa/setup")
    def mfa_setup(request: Request) -> dict:
        user = kit.require(request, allow_pending=True)
        if own_row(kit, user)["totp_enabled"]:
            raise HTTPException(409, "two-factor sign-in is already on; turn it off first to set up a new device")
        new = mfa.new_secret()
        with kit.ctx.db.transaction():
            mfa.store_pending_secret(kit.ctx.db, user.subject, new, kit.secret())
            audit_log.record(kit.ctx.db, user.subject, "security", "mfa_setup", user.subject, {}, kit.ip_of(request))
        return {"secret": new, "otpauth_uri": mfa.otpauth_uri(new, user.subject)}

    @app.post("/v1/security/mfa/enable")
    def mfa_enable(body: CodeBody, request: Request) -> JSONResponse:
        user = kit.require(request, allow_pending=True)
        return enable_two_factor(kit, request, user, body.code)

    @app.post("/v1/security/mfa/recovery-codes")
    def mfa_new_recovery_codes(body: CodeBody, request: Request) -> JSONResponse:
        user = kit.require(request)
        row = own_row(kit, user)
        if not row["totp_enabled"]:
            raise HTTPException(409, "two-factor sign-in is not on")
        verified(kit, user, row, body.code)
        codes = mfa.replace_recovery_codes(kit.ctx.db, user.subject, kit.secret())
        audit_log.record(kit.ctx.db, user.subject, "security", "recovery_codes_regenerated", user.subject, {},
                         kit.ip_of(request))
        return JSONResponse({"recovery_codes": codes}, headers={"Cache-Control": "no-store"})

    @app.post("/v1/security/mfa/disable")
    def mfa_disable(body: CodeBody, request: Request) -> dict:
        user = kit.require(request)
        row = own_row(kit, user)
        if not row["totp_enabled"]:
            raise HTTPException(409, "two-factor sign-in is not on")
        if require_mfa(kit.ctx) and mfa_role(row.get("roles") or row["role"]):
            raise HTTPException(403, "administrators must keep two-factor sign-in on")
        verified(kit, user, row, body.code)
        mfa.clear(kit.ctx.db, user.subject)
        audit_log.record(kit.ctx.db, user.subject, "security", "mfa_disabled", user.subject, {}, kit.ip_of(request))
        return {"enabled": False}
