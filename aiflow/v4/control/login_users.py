"""The authenticated console user list and account creation routes."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import audit as audit_log
from .application_context import ApplicationContext
from .console_auth import COOKIE_NAME, has_permission, resolve_session_user, verify_session
from .emails import normalize_email
from .login import client_ip, seed_user
from .login_sessions import user_json
from .login_signin import audit_console_event


@dataclass(frozen=True)
class UsersParts:
    ctx: ApplicationContext
    settings: object


def users_manager(ctx, settings, request: Request):
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret)
    user = resolve_session_user(ctx.db, claims) if claims else None
    if not user:
        return JSONResponse({"detail": "authenticated console session required"}, status_code=401)
    if not has_permission(user.effective_roles, "users.manage"):
        return JSONResponse({"detail": "owner role required"}, status_code=403)
    return user


def install_users(app: FastAPI, ctx: ApplicationContext) -> None:
    parts = UsersParts(ctx, ctx.settings)

    @app.get("/v1/auth/users")
    def console_users(request: Request) -> JSONResponse:
        user = users_manager(parts.ctx, parts.settings, request)
        if isinstance(user, JSONResponse):
            return user
        from . import licensing

        rows = parts.ctx.db.all("SELECT email,display_name,role,disabled,created_at FROM console_users ORDER BY email")
        state = licensing.snapshot(parts.ctx.db, parts.settings.license_public_key)
        count = int(parts.ctx.db.one("SELECT count(*) AS count FROM console_users WHERE disabled=false")["count"])
        users = [user_json(row["email"], row["role"], row["display_name"])
                 | {"disabled": row["disabled"], "created_at": row["created_at"].isoformat()} for row in rows]
        return JSONResponse({"users": users, "count": count, "limit": state["limits"]["users"]})

    @app.post("/v1/auth/users")
    async def create_console_user(request: Request) -> JSONResponse:
        user = users_manager(parts.ctx, parts.settings, request)
        if isinstance(user, JSONResponse):
            return user
        try:
            data = await request.json()
        except ValueError:
            data = None
        if (not isinstance(data, dict) or not isinstance(data.get("email"), str)
                or not isinstance(data.get("role"), str)):
            return JSONResponse({"detail": "Enter an email address and choose a role."}, status_code=422)
        display_name = data.get("display_name", "")
        if not isinstance(display_name, str) or len(display_name) > 120:
            return JSONResponse({"detail": "Display name must be 120 characters or fewer."}, status_code=422)
        try:
            email = normalize_email(data["email"])
            password = seed_user(parts.ctx.db, email, data["role"], display_name,
                                 public_key=parts.settings.license_public_key)
        except ValueError as exc:
            from . import licensing

            if isinstance(exc, licensing.LicenseError):
                status = 423 if "read-only" in str(exc).lower() else 409
            else:
                status = 422
            return JSONResponse({"detail": str(exc)}, status_code=status)
        if password is None:
            return JSONResponse({"detail": "A console user with this email already exists."}, status_code=409)
        audit_console_event(parts.ctx, "create_user", email, True, "created")
        audit_log.record(parts.ctx.db, user.subject, "people", "person_added", email, {"roles": [data["role"]]},
                         client_ip(request, parts.settings.trusted_proxies))
        return JSONResponse({"email": email, "name": display_name.strip(), "role": data["role"],
                             "initial_password": password,
                             "message": "Account created. Copy the initial password and share it securely."},
                            status_code=201, headers={"Cache-Control": "no-store"})
