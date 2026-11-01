"""Cookie handling, session revocation, and the signed-in identity endpoints."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import audit as audit_log, console_auth
from .application_context import ApplicationContext
from .console_auth import (COOKIE_NAME, ROLE_LABELS, csrf_token, landing_page, normalize_roles,
                           resolve_session_user, verify_session)

log = logging.getLogger(__name__)

CSRF_COOKIE = "aiflow4_csrf"
SESSION_TTL_S = 12 * 3600
SESSION_TTL_MIN_S, SESSION_TTL_MAX_S = 5 * 60, 30 * 24 * 3600


@dataclass(frozen=True)
class SessionParts:
    ctx: ApplicationContext
    settings: object


def secure_cookie(settings) -> bool:
    return str(settings.public_url or "").lower().startswith("https://")


def set_session_cookies(settings, response, token: str, ttl: int) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=ttl, httponly=True, secure=secure_cookie(settings),
                        samesite="strict", path="/")
    # The console script echoes this cookie in X-CSRF-Token; it only works with this session's cookie.
    response.set_cookie(CSRF_COOKIE, csrf_token(token, settings.console_session_secret), max_age=ttl, httponly=False,
                        secure=secure_cookie(settings), samesite="strict", path="/")


def clear_session_cookies(settings, response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=secure_cookie(settings), samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure_cookie(settings), samesite="strict")


def end_session(ctx, settings, request: Request, actor_hint: str = "") -> str:
    """Mark the session named by the request cookie as ended; return who it belonged to."""
    user = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret, allow_pending=True)
    if user and user.sid:
        ctx.db.run("UPDATE console_sessions SET revoked_at=now(), revoked_by=%s WHERE sid=%s AND revoked_at IS NULL",
                   (user.subject, user.sid))
        audit_log.record(ctx.db, user.subject, "auth", "signed_out", user.subject)
    return user.subject if user else ""


def session_revoked(ctx, sid: str) -> bool:
    """Fail closed when a session was ended, expired, unknown, or cannot be checked."""
    try:
        return ctx.db.one("SELECT 1 AS x FROM console_sessions WHERE sid=%s AND revoked_at IS NULL"
                          " AND expires_at > now()", (sid,)) is None
    except Exception:  # noqa: BLE001
        log.exception("console session check failed")
        return True


def register_session_revocation(ctx) -> None:
    secret = ctx.settings.console_session_secret
    if len(secret.encode()) >= 32:
        console_auth.SESSION_REVOKED[secret] = partial(session_revoked, ctx)


def user_json(email: str, roles: list[str] | tuple[str, ...], name: str = "") -> dict:
    assigned = normalize_roles(roles)
    return {"email": email, "name": name, "role": assigned[0], "role_label": ROLE_LABELS[assigned[0]],
            "roles": list(assigned), "role_labels": [ROLE_LABELS[role] for role in assigned],
            "landing_page": landing_page(assigned)}


def current_user_json(ctx, settings, request: Request) -> JSONResponse:
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret, allow_pending=True)
    user = resolve_session_user(ctx.db, claims, True) if claims else None
    if not user:
        return JSONResponse({"detail": "not signed in"}, status_code=401)
    row = ctx.db.one("SELECT display_name FROM console_users WHERE email=%s", (user.subject,))
    data = user_json(user.subject, list(user.assigned_roles), (row or {}).get("display_name", ""))
    data["landing_page"] = landing_page(user.effective_roles)
    data["mfa_setup_required"] = user.mfa_pending
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


def install_sessions(app: FastAPI, ctx: ApplicationContext) -> None:
    parts = SessionParts(ctx, ctx.settings)

    @app.post("/v1/auth/logout")
    def logout(request: Request) -> JSONResponse:
        end_session(parts.ctx, parts.settings, request)
        response = JSONResponse({"signed_out": True})
        clear_session_cookies(parts.settings, response)
        return response

    @app.post("/logout", include_in_schema=False)
    def logout_page(request: Request) -> RedirectResponse:
        end_session(parts.ctx, parts.settings, request)
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookies(parts.settings, response)
        return response

    @app.get("/v1/auth/me")
    def me(request: Request) -> JSONResponse:
        return current_user_json(parts.ctx, parts.settings, request)

    app.state.set_session_cookies = partial(set_session_cookies, parts.settings)
