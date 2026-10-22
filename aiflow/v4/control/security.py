"""Security hardening of the control API (docs/v4/security.md).

Installed once by `create_app` after the console login (login.py), it adds:
- rate limits (AC3): a per-client-IP budget on every endpoint, and a tighter per-path budget on public endpoints
  (the ones that need no sign-in: login, install, join, provider callbacks, health) and on anonymous changes; a request
  over budget gets 429 with `Retry-After`. Sign-in guessing has its own protection in login.py;
- cross-site request protection (AC4): a state-changing request that rides a console session cookie must carry the
  `X-CSRF-Token` of that session (the console script copies it from the `aiflow4_csrf` cookie). Machine callers that
  send an `Authorization` header are not affected: a browser cannot add that header to a cross-site request;
- security headers, including a content security policy that blocks scripts from other origins;
- the audit trail (AC7): every successful state-changing request of a signed-in person is recorded;
- the security screen's API under `/v1/security/*`: two-factor setup and recovery codes, session length, forced
  sign-out, people and the read-only audit log (`GET /v1/security/audit`; there is no route that writes to it).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..core import secretbox
from . import audit as audit_log
from . import mfa
from .console_auth import COOKIE_NAME, ConsoleUser, csrf_token, has_permission, issue_session, verify_session
from .login import (CSRF_COOKIE, MFA_ROLES, OPEN_PATHS, OPEN_PREFIXES, SESSION_TTL_MAX_S, SESSION_TTL_MIN_S, client_ip,
                    get_setting, require_mfa, session_ttl_s, set_setting)

log = logging.getLogger(__name__)

CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT = {"/v1/auth/login"}  # no session yet; the password (and second factor) is the credential
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: https:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class SettingsBody(BaseModel):
    session_ttl_s: int | None = Field(default=None, ge=SESSION_TTL_MIN_S, le=SESSION_TTL_MAX_S)
    require_mfa: bool | None = None


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)


def install_security(app: FastAPI, ctx) -> None:
    settings = ctx.settings
    secret = lambda: settings.console_session_secret  # noqa: E731 - read late: tests and the CLI set it after startup

    def session(request: Request, allow_pending: bool = False) -> ConsoleUser | None:
        return verify_session(request.cookies.get(COOKIE_NAME, ""), secret(), None, allow_pending)

    def require(request: Request, permission: str = "", allow_pending: bool = False) -> ConsoleUser:
        if len(secret().encode()) < 32:
            raise HTTPException(503, "console sign-in is not configured")
        user = session(request, allow_pending)
        if not user:
            raise HTTPException(401, "sign in required")
        if permission and not has_permission(user.role, permission):
            raise HTTPException(403, f"your role cannot do this ({permission} is required)")
        return user

    def ip_of(request: Request) -> str:
        return client_ip(request, settings.trusted_proxies)

    # --- middleware (the last one registered is the outermost) --------------------------------------------------------
    @app.middleware("http")
    async def audit_changes(request: Request, call_next):
        response = await call_next(request)
        if request.method in SAFE_METHODS or response.status_code >= 400 or request.url.path.startswith("/v1/security"):
            return response
        user = await run_in_threadpool(session, request, True)
        if user:
            route = request.scope.get("route")
            template = getattr(route, "path", request.url.path)
            await run_in_threadpool(audit_log.record, ctx.db, user.subject, audit_log.category_for(request.method, template),
                                    f"{request.method} {template}", "", {"status": response.status_code}, ip_of(request))
        return response

    @app.middleware("http")
    async def csrf(request: Request, call_next):
        if request.method in SAFE_METHODS or request.url.path in CSRF_EXEMPT:
            return await call_next(request)
        cookie = request.cookies.get(COOKIE_NAME, "")
        if cookie and not request.headers.get("authorization"):
            given = request.headers.get(CSRF_HEADER, "")
            if not hmac.compare_digest(given.encode(), csrf_token(cookie, secret()).encode()):
                return JSONResponse({"detail": "missing or invalid CSRF token"}, status_code=403)
        return await call_next(request)

    def spend(ip: str, name: str, limit: int) -> int:
        """Count this request in the current minute; the seconds to wait when the budget is spent, else 0."""
        window = int(time.time()) // 60
        k = f"{ctx.leases.p}rl:{hashlib.sha256(f'{ip}\n{name}'.encode()).hexdigest()[:32]}:{window}"
        pipe = ctx.leases.r.pipeline()
        pipe.incr(k)
        pipe.expire(k, 90)
        count = int(pipe.execute()[0])
        return max(1, 60 - int(time.time()) % 60) if count > limit else 0

    def over_budget(request: Request) -> int:
        path, ip = request.url.path, ip_of(request)
        budgets = []
        if settings.rate_limit_per_min:
            budgets.append(("all", settings.rate_limit_per_min))
        public = path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)
        anonymous_change = (request.method not in SAFE_METHODS and not request.cookies.get(COOKIE_NAME)
                            and not request.headers.get("authorization"))
        if settings.rate_limit_public_per_min and (public or anonymous_change):
            budgets.append(("path:" + path, settings.rate_limit_public_per_min))
        for name, limit in budgets:
            wait = spend(ip, name, limit)
            if wait:
                return wait
        return 0

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        try:
            wait = await run_in_threadpool(over_budget, request)
        except redis.RedisError:
            log.warning("rate limits: Redis is unavailable; requests are not limited meanwhile")
            wait = 0
        if wait:
            response = JSONResponse({"detail": "too many requests; try again later"}, status_code=429,
                                    headers={"Retry-After": str(wait)})
        else:
            response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    # --- settings, sessions, people -----------------------------------------------------------------------------------
    def settings_view() -> dict:
        return {"session_ttl_s": session_ttl_s(ctx), "session_ttl_min_s": SESSION_TTL_MIN_S,
                "session_ttl_max_s": SESSION_TTL_MAX_S, "require_mfa": require_mfa(ctx), "mfa_roles": list(MFA_ROLES),
                "rate_limit_per_min": settings.rate_limit_per_min,
                "rate_limit_public_per_min": settings.rate_limit_public_per_min,
                "secret_key_configured": _key_configured()}

    def _key_configured() -> bool:
        try:
            return secretbox.load_key() is not None
        except secretbox.SecretBoxError:
            return False

    @app.get("/v1/security/settings")
    def get_security_settings(request: Request) -> dict:
        require(request, "security.manage")
        return settings_view()

    @app.put("/v1/security/settings")
    def put_security_settings(request: Request, body: SettingsBody) -> dict:
        user = require(request, "security.manage")
        changes: dict[str, Any] = {}
        if body.session_ttl_s is not None:
            changes["session_ttl_s"] = body.session_ttl_s
        if body.require_mfa is not None:
            changes["require_mfa"] = body.require_mfa
        for name, value in changes.items():
            before = settings_view()[name]
            set_setting(ctx.db, name, ("1" if value else "0") if isinstance(value, bool) else str(value), user.subject)
            audit_log.record(ctx.db, user.subject, "settings", f"security_setting_{name}", name,
                             {"from": before, "to": value}, ip_of(request))
        return settings_view()

    @app.get("/v1/security/sessions")
    def list_sessions(request: Request) -> dict:
        user = require(request, "security.manage")
        rows = ctx.db.all("SELECT sid, email, role, ip, created_at, expires_at FROM console_sessions"
                          " WHERE revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC LIMIT 200")
        for r in rows:
            r["created_at"], r["expires_at"] = r["created_at"].isoformat(), r["expires_at"].isoformat()
            r["current"] = r["sid"] == user.sid
        return {"sessions": rows}

    def revoke(request: Request, user: ConsoleUser, where: str, params: tuple, target: str) -> int:
        n = ctx.db.run(f"UPDATE console_sessions SET revoked_at=now(), revoked_by=%s WHERE revoked_at IS NULL AND {where}",
                       (user.subject, *params))
        audit_log.record(ctx.db, user.subject, "security", "forced_sign_out", target, {"sessions": n}, ip_of(request))
        return n

    @app.post("/v1/security/sessions/{sid}/revoke")
    def revoke_session(sid: str, request: Request) -> dict:
        user = require(request, "security.manage")
        row = ctx.db.one("SELECT email FROM console_sessions WHERE sid=%s", (sid,))
        if not row:
            raise HTTPException(404, "unknown session")
        return {"revoked": revoke(request, user, "sid=%s", (sid,), row["email"])}

    def known_user(email: str) -> dict:
        row = ctx.db.one("SELECT email, role FROM console_users WHERE email=%s", (email.strip().lower(),))
        if not row:
            raise HTTPException(404, "unknown person")
        return row

    @app.post("/v1/security/users/{email}/sign-out")
    def sign_out_user(email: str, request: Request) -> dict:
        user = require(request, "security.manage")
        row = known_user(email)
        return {"revoked": revoke(request, user, "email=%s", (row["email"],), row["email"])}

    @app.get("/v1/security/users")
    def list_users(request: Request) -> dict:
        require(request, "security.manage")
        rows = ctx.db.all(
            "SELECT u.email, u.display_name, u.role, u.disabled, u.totp_enabled, u.last_login_at,"
            " (u.locked_until IS NOT NULL AND u.locked_until > now()) AS locked,"
            " (SELECT count(*) FROM console_recovery_codes r WHERE r.email=u.email AND r.used_at IS NULL) AS recovery_codes_left,"
            " (SELECT count(*) FROM console_sessions s WHERE s.email=u.email AND s.revoked_at IS NULL AND s.expires_at>now()) AS sessions"
            " FROM console_users u ORDER BY u.email")
        for r in rows:
            r["last_login_at"] = r["last_login_at"].isoformat() if r["last_login_at"] else None
            r["recovery_codes_left"], r["sessions"] = int(r["recovery_codes_left"]), int(r["sessions"])
        return {"users": rows}

    @app.post("/v1/security/users/{email}/mfa/reset")
    def reset_user_mfa(email: str, request: Request) -> dict:
        """A person lost the authenticator and every recovery code: an authorised user clears their second factor."""
        user = require(request, "security.manage")
        row = known_user(email)
        mfa.clear(ctx.db, row["email"])
        revoke(request, user, "email=%s", (row["email"],), row["email"])
        audit_log.record(ctx.db, user.subject, "people", "mfa_reset", row["email"], {}, ip_of(request))
        return {"reset": True}

    @app.post("/v1/security/users/{email}/unlock")
    def unlock_user(email: str, request: Request) -> dict:
        user = require(request, "security.manage")
        row = known_user(email)
        ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, updated_at=now() WHERE email=%s",
                   (row["email"],))
        audit_log.record(ctx.db, user.subject, "people", "account_unlocked", row["email"], {}, ip_of(request))
        return {"unlocked": True}

    # --- two-factor setup (any signed-in person, for their own account) ---------------------------------------------
    def own_row(user: ConsoleUser) -> dict:
        row = ctx.db.one("SELECT email, role, totp_secret, totp_enabled FROM console_users WHERE email=%s", (user.subject,))
        if not row:
            raise HTTPException(404, "unknown person")
        return row

    def verified(user: ConsoleUser, row: dict, code: str) -> str:
        factor = mfa.check_second_factor(ctx.db, row, user.subject, code, secret())
        if not factor:
            raise HTTPException(403, "that code is not valid")
        return factor

    @app.get("/v1/security/mfa")
    def mfa_status(request: Request) -> dict:
        user = require(request, allow_pending=True)
        row = own_row(user)
        return {"enabled": bool(row["totp_enabled"]), "recovery_codes_left": mfa.recovery_codes_left(ctx.db, user.subject),
                "required": require_mfa(ctx) and row["role"] in MFA_ROLES, "setup_required": user.mfa_pending,
                "can_manage_security": has_permission(user.role, "security.manage")}

    @app.post("/v1/security/mfa/setup")
    def mfa_setup(request: Request) -> dict:
        user = require(request, allow_pending=True)
        if own_row(user)["totp_enabled"]:
            raise HTTPException(409, "two-factor sign-in is already on; turn it off first to set up a new device")
        new = mfa.new_secret()
        mfa.store_pending_secret(ctx.db, user.subject, new, secret())
        return {"secret": new, "otpauth_uri": mfa.otpauth_uri(new, user.subject)}

    @app.post("/v1/security/mfa/enable")
    def mfa_enable(body: CodeBody, request: Request) -> JSONResponse:
        user = require(request, allow_pending=True)
        row = own_row(user)
        if row["totp_enabled"] or not row["totp_secret"]:
            raise HTTPException(409, "start the setup first")
        step = mfa.match_step(mfa.read_secret(row, user.subject, secret()), body.code)
        if step is None:
            raise HTTPException(403, "that code is not valid; check the time on your phone and try again")
        ctx.db.run("UPDATE console_users SET totp_enabled=true, totp_last_step=%s, updated_at=now() WHERE email=%s",
                   (step, user.subject))
        codes = mfa.replace_recovery_codes(ctx.db, user.subject, secret())
        audit_log.record(ctx.db, user.subject, "security", "mfa_enabled", user.subject, {}, ip_of(request))
        response = JSONResponse({"enabled": True, "recovery_codes": codes}, headers={"Cache-Control": "no-store"})
        if user.mfa_pending:  # the limited session becomes a full one on the same session row
            ttl = session_ttl_s(ctx)
            token = issue_session(user.subject, user.role, int(time.time()) + ttl, secret(), user.sid)
            app.state.set_session_cookies(response, token, ttl)
        return response

    @app.post("/v1/security/mfa/recovery-codes")
    def mfa_new_recovery_codes(body: CodeBody, request: Request) -> JSONResponse:
        user = require(request)
        row = own_row(user)
        if not row["totp_enabled"]:
            raise HTTPException(409, "two-factor sign-in is not on")
        verified(user, row, body.code)
        codes = mfa.replace_recovery_codes(ctx.db, user.subject, secret())
        audit_log.record(ctx.db, user.subject, "security", "recovery_codes_regenerated", user.subject, {}, ip_of(request))
        return JSONResponse({"recovery_codes": codes}, headers={"Cache-Control": "no-store"})

    @app.post("/v1/security/mfa/disable")
    def mfa_disable(body: CodeBody, request: Request) -> dict:
        user = require(request)
        row = own_row(user)
        if not row["totp_enabled"]:
            raise HTTPException(409, "two-factor sign-in is not on")
        if require_mfa(ctx) and row["role"] in MFA_ROLES:
            raise HTTPException(403, "administrators must keep two-factor sign-in on")
        verified(user, row, body.code)
        mfa.clear(ctx.db, user.subject)
        audit_log.record(ctx.db, user.subject, "security", "mfa_disabled", user.subject, {}, ip_of(request))
        return {"enabled": False}

    # --- audit log: read only ---------------------------------------------------------------------------------------
    @app.get("/v1/security/audit")
    def read_audit(request: Request, category: str = "", actor: str = "", before: int = 0, limit: int = 100) -> dict:
        require(request, "security.audit")
        if category and category not in audit_log.CATEGORIES:
            raise HTTPException(400, f"category must be one of {', '.join(audit_log.CATEGORIES)}")
        return {"entries": audit_log.listing(ctx.db, category, actor, before, limit), "categories": list(audit_log.CATEGORIES)}
