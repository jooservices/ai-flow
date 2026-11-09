"""Routes of console sign-in, sessions and user management, moved out of `login_signin.py`, `login_sessions.py` and
`login_users.py` as they were. `install` registers them in the same order; the permission gate and the security
middleware stay in `login.install_login`."""
from __future__ import annotations

import logging
import time
from functools import partial

import redis
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ... import setup_wizard
from ...console_auth import COOKIE_NAME, has_permission, issue_session, resolve_session_user, verify_session
from ...emails import normalize_email
from ...login import LOGIN_PAGE, client_ip, preauth_origin_allowed
from . import service
from .session_cookies import SESSION_TTL_S, clear_session_cookies, set_session_cookies

log = logging.getLogger(__name__)


def credentials_from(data) -> tuple[str, str, str] | None:
    """The normalized email, the password and the second-factor code of a sign-in body, or None when it is malformed."""
    if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
        return None
    try:
        email = normalize_email(data["email"])
    except ValueError:
        email = ""
    code = data.get("code") if isinstance(data.get("code"), str) else ""
    return email, data["password"][:1024], code[:64]


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
    credentials = credentials_from(data)
    if credentials is None:
        return JSONResponse({"detail": "enter your email and password"}, status_code=400)
    email, password, code = credentials
    try:
        status, body, headers = await run_in_threadpool(
            service.attempt_signin, ctx, settings, client_ip(request, settings.trusted_proxies), email, password, code)
    except redis.RedisError:
        log.exception("console sign-in: Redis is unavailable")
        return JSONResponse({"detail": "sign-in is unavailable; try again shortly"}, status_code=503)
    sid, ttl = body.pop("_sid", ""), body.pop("_ttl", SESSION_TTL_S)
    response = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **headers})
    if status == 200:
        service.register_session_revocation(ctx)
        token = issue_session(email, body["roles"], int(time.time()) + ttl, secret, sid=sid,
                              mfa_pending=body["mfa_setup_required"])
        set_session_cookies(settings, response, token, ttl)
    return response


def users_manager(ctx, settings, request: Request):
    """The signed-in user allowed to manage users, or the 401/403 response to return."""
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret)
    user = resolve_session_user(ctx.db, claims) if claims else None
    if not user:
        return JSONResponse({"detail": "authenticated console session required"}, status_code=401)
    if not has_permission(user.effective_roles, "users.manage"):
        return JSONResponse({"detail": "owner role required"}, status_code=403)
    return user


async def create_user_response(ctx, settings, request: Request) -> JSONResponse:
    user = users_manager(ctx, settings, request)
    if isinstance(user, JSONResponse):
        return user
    try:
        data = await request.json()
    except ValueError:
        data = None
    status, body = service.create_console_user(ctx, settings, user.subject, data,
                                               client_ip(request, settings.trusted_proxies))
    headers = {"Cache-Control": "no-store"} if status == 201 else None
    return JSONResponse(body, status_code=status, headers=headers)


def install(app, ctx, deps=None) -> None:
    """Register the routes; `deps` is unused and only keeps the signature every feature installs with."""
    settings = ctx.settings

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> HTMLResponse:
        if setup_wizard.user_count(ctx) == 0:
            return setup_wizard.setup_form_response(request, ctx)
        return HTMLResponse(LOGIN_PAGE, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post("/v1/auth/login")
    async def login(request: Request) -> JSONResponse:
        return await login_response(ctx, request)

    @app.post("/v1/auth/logout")
    def logout(request: Request) -> JSONResponse:
        service.end_session(ctx, settings, request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse({"signed_out": True})
        clear_session_cookies(settings, response)
        return response

    @app.post("/logout", include_in_schema=False)
    def logout_page(request: Request) -> RedirectResponse:
        service.end_session(ctx, settings, request.cookies.get(COOKIE_NAME, ""))
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookies(settings, response)
        return response

    @app.get("/v1/auth/me")
    def me(request: Request) -> JSONResponse:
        data = service.current_user(ctx, settings, request.cookies.get(COOKIE_NAME, ""))
        if data is None:
            return JSONResponse({"detail": "not signed in"}, status_code=401)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    app.state.set_session_cookies = partial(set_session_cookies, settings)

    @app.get("/v1/auth/users")
    def console_users(request: Request) -> JSONResponse:
        user = users_manager(ctx, settings, request)
        if isinstance(user, JSONResponse):
            return user
        return JSONResponse(service.list_console_users(ctx, settings))

    @app.post("/v1/auth/users")
    async def create_console_user(request: Request) -> JSONResponse:
        return await create_user_response(ctx, settings, request)
