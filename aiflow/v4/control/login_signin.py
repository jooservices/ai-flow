"""Sign-in page, credential checks, rate limits, and failed sign-in audit rows."""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

import redis
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import audit as audit_log, mfa
from .application_context import ApplicationContext
from .console_auth import issue_session
from .db import J
from .emails import normalize_email
from . import login
from .login import (LOGIN_PAGE, LOCK_S, MAX_FAILURES, RATE_WINDOW_S, REFUSED, SESSION_TTL_S, _dummy_hash, client_ip,
                    mfa_role, preauth_origin_allowed, require_mfa, session_ttl_s)
from .login_sessions import register_session_revocation, set_session_cookies, user_json

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignInParts:
    ctx: ApplicationContext
    settings: object


@dataclass(frozen=True)
class AttemptState:
    row: object | None
    locked: bool
    enrolled: bool
    second_factor: bool
    usable: bool
    good: bool
    factor: str


def rate_limit_key(prefix: str, *parts: str) -> str:
    return prefix + "console_login:" + ":".join(parts)


def rate_limit_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def audit_console_event(ctx, action: str, email: str, accepted: bool, result: str) -> None:
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
               " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               ("console", action, "", "", None, email, accepted, result, J({})))


def audit_failed_signin(ctx, email: str, result: str) -> None:
    bucket = rate_limit_key(ctx.leases.p, "audit", str(int(time.time()) // 60))
    pipe = ctx.leases.r.pipeline()
    pipe.incr(bucket)
    pipe.expire(bucket, 120)
    count = int(pipe.execute()[0])
    if count <= login.AUDIT_FAILED_PER_MIN:
        audit_console_event(ctx, "login", email, False, result)
    elif count == login.AUDIT_FAILED_PER_MIN + 1:
        log.warning("console sign-in: more than %s failures this minute; further failures are not audited",
                    login.AUDIT_FAILED_PER_MIN)


def over_limit(ctx, ip: str, email: str) -> int:
    """Count an attempt; return the remaining wait when either sign-in budget is spent."""
    redis_client = ctx.leases.r
    keys = ((rate_limit_key(ctx.leases.p, "ip", rate_limit_digest(ip)), login.RATE_PER_IP),
            (rate_limit_key(ctx.leases.p, "ip_email", rate_limit_digest(f"{ip}\n{email}")), login.RATE_PER_IP_EMAIL))
    pipe = redis_client.pipeline()
    for key, _limit in keys:
        pipe.set(key, 0, ex=RATE_WINDOW_S, nx=True)
        pipe.incr(key)
    counts = pipe.execute()[1::2]
    for (key, limit), count in zip(keys, counts, strict=False):
        if int(count) > limit:
            return max(1, int(redis_client.ttl(key)))
    return 0


def find_console_user(ctx, email: str):
    if not email:
        return None
    ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, updated_at=now()"
               " WHERE email=%s AND locked_until IS NOT NULL AND locked_until <= now()", (email,))
    return ctx.db.one("SELECT email, display_name, role, roles, password_hash, disabled, locked_until,"
                      " totp_secret, totp_enabled FROM console_users WHERE email=%s", (email,))


def limited_signin_response(limited: bool, usable: bool, good: bool, enrolled: bool, factor: str,
                            wait: int) -> tuple[int, dict, dict] | None:
    if limited and not (usable and good and enrolled and factor):
        return 429, {"detail": "too many sign-in attempts; try again later"}, {"Retry-After": str(wait)}
    return None


def failed_signin_response(ctx, email: str, usable: bool, locked: bool, second_factor: bool):
    if usable and not locked:
        ctx.db.run("UPDATE console_users SET failed_attempts=failed_attempts+1,"
                   " locked_until=CASE WHEN failed_attempts+1 >= %s THEN now() + make_interval(secs => %s)"
                   " ELSE locked_until END, updated_at=now() WHERE email=%s", (MAX_FAILURES, LOCK_S, email))
    audit_failed_signin(ctx, email, "account locked" if locked and not second_factor else REFUSED)
    return 401, {"detail": REFUSED}, {}


def successful_signin(ctx, settings, ip: str, email: str, row, factor: str):
    ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, last_login_at=now(), updated_at=now()"
               " WHERE email=%s", (email,))
    ctx.leases.r.delete(rate_limit_key(ctx.leases.p, "ip_email",
                                       rate_limit_digest(f"{ip}\n{email}")))
    audit_console_event(ctx, "login", email, True, "signed in")
    roles = row.get("roles") or [row["role"]]
    pending = mfa_role(roles) and not row["totp_enabled"] and require_mfa(ctx)
    ttl = session_ttl_s(ctx)
    sid = secrets.token_urlsafe(18)
    ctx.db.run("INSERT INTO console_sessions (sid, email, role, ip, expires_at)"
               " VALUES (%s,%s,%s,%s, now() + make_interval(secs => %s))", (sid, email, row["role"], ip, ttl))
    audit_log.record(ctx.db, email, "auth", "signed_in", email,
                     {"second_factor": factor or "none", "mfa_setup_required": pending}, ip)
    body = user_json(email, roles, row["display_name"])
    body.update({"mfa_setup_required": pending, "_sid": sid, "_ttl": ttl})
    if factor == "recovery":
        body["recovery_codes_left"] = mfa.recovery_codes_left(ctx.db, email)
    return 200, body, {}


def prepare_signin(ctx, settings, email: str, password: str, code: str) -> AttemptState:
    row = find_console_user(ctx, email)
    locked = bool(row and row["locked_until"])
    enrolled = bool(row and row["totp_enabled"])
    second_factor = bool(code.strip()) and enrolled
    usable = bool(row and not row["disabled"] and (not locked or second_factor))
    good = login.verify_password(password, row["password_hash"] if usable else _dummy_hash())
    factor = ""
    if usable and good and enrolled and code.strip():
        factor = mfa.check_second_factor(ctx.db, row, email, code, settings.console_session_secret)
        good = bool(factor)
    return AttemptState(row, locked, enrolled, second_factor, usable, good, factor)


def needs_second_factor_code(state: AttemptState, code: str) -> bool:
    return state.usable and state.good and state.enrolled and not code.strip()


def check_signin(ctx, settings, ip: str, email: str, password: str, code: str, wait: int,
                 limited: bool) -> tuple[int, dict, dict]:
    state = prepare_signin(ctx, settings, email, password, code)
    limited_response = limited_signin_response(limited, state.usable, state.good, state.enrolled, state.factor, wait)
    if limited_response:
        return limited_response
    if needs_second_factor_code(state, code):
        return 401, {"detail": "enter the code from your authenticator app or a recovery code",
                     "mfa_required": True}, {}
    if not (state.usable and state.good):
        return failed_signin_response(ctx, email, state.usable, state.locked, state.second_factor)
    return successful_signin(ctx, settings, ip, email, state.row, state.factor)


def attempt_signin(ctx, settings, ip: str, email: str, password: str, code: str = "") -> tuple[int, dict, dict]:
    """Run the blocking sign-in work in a worker thread, keeping rate and DB changes in the original order."""
    wait = over_limit(ctx, ip, email)
    with ctx.db.transaction():
        return check_signin(ctx, settings, ip, email, password, code, wait, bool(wait))


async def login_response(ctx, request: Request) -> JSONResponse:
    settings = ctx.settings
    secret = settings.console_session_secret
    if len(secret.encode()) < 32:
        return JSONResponse({"detail": "console sign-in is not configured"}, status_code=503)
    if not preauth_origin_allowed(request, settings.public_url):
        return JSONResponse({"detail": "cross-site sign-in request refused"}, status_code=403)
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
        return JSONResponse({"detail": "enter your email and password"}, status_code=400)
    try:
        email = normalize_email(data["email"])
    except ValueError:
        email = ""
    try:
        code = data.get("code") if isinstance(data.get("code"), str) else ""
        status, body, headers = await run_in_threadpool(
            attempt_signin, ctx, settings, client_ip(request, settings.trusted_proxies),
            email, data["password"][:1024], code[:64])
    except redis.RedisError:
        log.exception("console sign-in: Redis is unavailable")
        return JSONResponse({"detail": "sign-in is unavailable; try again shortly"}, status_code=503)
    sid, ttl = body.pop("_sid", ""), body.pop("_ttl", SESSION_TTL_S)
    response = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **headers})
    if status == 200:
        register_session_revocation(ctx)
        token = issue_session(email, body["roles"], int(time.time()) + ttl, secret, sid=sid,
                              mfa_pending=body["mfa_setup_required"])
        set_session_cookies(settings, response, token, ttl)
    return response


def install_signin(app: FastAPI, ctx: ApplicationContext) -> None:
    from . import setup_wizard

    parts = SignInParts(ctx, ctx.settings)

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> HTMLResponse:
        if setup_wizard.user_count(parts.ctx) == 0:
            return setup_wizard.setup_form_response(request, parts.ctx)
        return HTMLResponse(LOGIN_PAGE, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

