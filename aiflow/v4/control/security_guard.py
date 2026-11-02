"""Shared pieces of the security installation (docs/v4/security-structure.md): constants and the request helpers
(`session`, `require`, `ip_of`) that the middleware and the `/v1/security/*` routes use."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from .console_auth import COOKIE_NAME, ConsoleUser, has_permission, resolve_session_user, verify_session
from .login import client_ip

CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT = {"/v1/auth/login"}  # checked against Origin/Referer in login.py before credentials are read
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: https:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class SecurityKit:
    """The application, its context and the request helpers every security part needs."""

    def __init__(self, app: FastAPI, ctx) -> None:
        self.app = app
        self.ctx = ctx
        self.settings = ctx.settings

    def secret(self) -> str:
        # read late: tests and the CLI set it after startup
        return self.settings.console_session_secret

    def session(self, request: Request, allow_pending: bool = False) -> ConsoleUser | None:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), self.secret(), None, allow_pending)
        return resolve_session_user(self.ctx.db, claims, allow_pending) if claims else None

    def require(self, request: Request, permission: str = "", allow_pending: bool = False) -> ConsoleUser:
        if len(self.secret().encode()) < 32:
            raise HTTPException(503, "console sign-in is not configured")
        user = self.session(request, allow_pending)
        if not user:
            raise HTTPException(401, "sign in required")
        if permission and not has_permission(user.effective_roles, permission):
            raise HTTPException(403, f"your role cannot do this ({permission} is required)")
        return user

    def ip_of(self, request: Request) -> str:
        return client_ip(request, self.settings.trusted_proxies)
