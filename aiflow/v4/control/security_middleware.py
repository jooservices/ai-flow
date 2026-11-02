"""The three security middleware: audit of changes, CSRF check, rate limits and security headers."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

import redis
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import audit as audit_log
from .console_auth import COOKIE_NAME, csrf_token
from .login import OPEN_PATHS, OPEN_PREFIXES
from .route_registry import concrete_routes
from .security_guard import CSP, CSRF_EXEMPT, CSRF_HEADER, SAFE_METHODS, SecurityKit

log = logging.getLogger(__name__)

CALLBACK_ACTORS = {
    "/v1/integrations/jira/oauth/callback": "jira-oauth",
    "/v1/integrations/github/manifest/callback": "github-oauth",
    "/v1/integrations/github/setup": "github-oauth",
}


def owner_token_actor(kit: SecurityKit, request: Request) -> str:
    """The Jira integration routes accept the owner bearer token; its changes are audited as `owner-token`."""
    scheme, separator, token = request.headers.get("authorization", "").partition(" ")
    owner_token = kit.settings.owner_token
    if (separator and scheme.lower() == "bearer" and token.strip() and owner_token
            and hmac.compare_digest(token.strip().encode(), owner_token.encode())):
        return "owner-token"
    return ""


async def audit_actor(kit: SecurityKit, request: Request) -> str:
    user = await run_in_threadpool(kit.session, request, True)
    actor = user.subject if user else ""
    if not actor and request.url.path.startswith("/v1/integrations/jira/"):
        actor = owner_token_actor(kit, request)
    return actor or CALLBACK_ACTORS.get(request.url.path, "")


def is_explicit_audit(path: str) -> bool:
    """People and role changes are recorded by their handlers with the person as target and the roles as detail."""
    return (path.startswith("/v1/security/") or path.startswith("/v1/console/users")
            or path.startswith("/v1/auth/") or path == "/logout")


def route_template(kit: SecurityKit, request: Request) -> str:
    template = request.url.path
    for route in concrete_routes(kit.app.routes):
        match, _ = route.matches(request.scope)
        if match.name == "FULL":
            return getattr(route, "path", template)
    return template


def install_audit_changes(kit: SecurityKit) -> None:
    @kit.app.middleware("http")
    async def audit_changes(request: Request, call_next):
        if request.method in SAFE_METHODS and request.url.path not in CALLBACK_ACTORS:
            return await call_next(request)
        actor = await audit_actor(kit, request)
        if not actor:
            return await call_next(request)
        with kit.ctx.db.transaction():
            if not is_explicit_audit(request.url.path):
                template = route_template(kit, request)
                # Insert before invoking code that may change a database row or a secret file. If the append fails,
                # the handler never runs.
                await run_in_threadpool(
                    audit_log.record, kit.ctx.db, actor, audit_log.category_for(request.method, template),
                    f"{request.method} {template}", "", {}, kit.ip_of(request))
            return await call_next(request)


def install_csrf(kit: SecurityKit) -> None:
    @kit.app.middleware("http")
    async def csrf(request: Request, call_next):
        if request.method in SAFE_METHODS or request.url.path in CSRF_EXEMPT:
            return await call_next(request)
        cookie = request.cookies.get(COOKIE_NAME, "")
        if cookie and not request.headers.get("authorization"):
            given = request.headers.get(CSRF_HEADER, "")
            if not hmac.compare_digest(given.encode(), csrf_token(cookie, kit.secret()).encode()):
                return JSONResponse({"detail": "missing or invalid CSRF token"}, status_code=403)
        return await call_next(request)


def spend(kit: SecurityKit, ip: str, name: str, limit: int) -> int:
    """Count this request in the current minute; the seconds to wait when the budget is spent, else 0."""
    window = int(time.time()) // 60
    k = f"{kit.ctx.leases.p}rl:{hashlib.sha256(f'{ip}\n{name}'.encode()).hexdigest()[:32]}:{window}"
    pipe = kit.ctx.leases.r.pipeline()
    pipe.incr(k)
    pipe.expire(k, 90)
    count = int(pipe.execute()[0])
    return max(1, 60 - int(time.time()) % 60) if count > limit else 0


def request_budgets(kit: SecurityKit, request: Request) -> list[tuple[str, int]]:
    settings, path = kit.settings, request.url.path
    budgets = []
    if settings.rate_limit_per_min:
        budgets.append(("all", settings.rate_limit_per_min))
    public = path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)
    anonymous_change = (request.method not in SAFE_METHODS and not request.cookies.get(COOKIE_NAME)
                        and not request.headers.get("authorization"))
    if settings.rate_limit_public_per_min and (public or anonymous_change):
        budgets.append(("path:" + path, settings.rate_limit_public_per_min))
    return budgets


def over_budget(kit: SecurityKit, request: Request) -> int:
    # Login has a credential-aware limiter: the owner can prove identity with a valid second factor even when
    # the guessing budgets are spent. A generic 429 here would bypass that exception.
    if request.url.path == "/v1/auth/login":
        return 0
    ip = kit.ip_of(request)
    for name, limit in request_budgets(kit, request):
        wait = spend(kit, ip, name, limit)
        if wait:
            return wait
    return 0


def add_security_headers(response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", CSP)


def install_limits_and_headers(kit: SecurityKit) -> None:
    @kit.app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        try:
            wait = await run_in_threadpool(over_budget, kit, request)
        except redis.RedisError:
            log.exception("rate limits: Redis is unavailable; refusing request")
            response = JSONResponse({"detail": "request protection is unavailable; try again shortly"},
                                    status_code=503, headers={"Retry-After": "5"})
        else:
            if wait:
                response = JSONResponse({"detail": "too many requests; try again later"}, status_code=429,
                                        headers={"Retry-After": str(wait)})
            else:
                response = await call_next(request)
        add_security_headers(response)
        return response
