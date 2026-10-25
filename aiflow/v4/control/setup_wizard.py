"""Browser first-run setup routes for a new control plane.

The first Administrator and public address are created together from the setup screen. Later setup actions reuse
the existing Integrations, Machines and AI, Projects and node-join APIs; the Dashboard keeps their readiness, skip,
resume and sample-ticket confirmation visible until every step is ready. This module owns HTTP concerns only
(transport security, the bootstrap challenge, authorization, request and response mapping); the decisions live in
`setup_service.py` and the SQL in `repositories/setup_wizard.py`.
"""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from . import setup_service
from .console_auth import COOKIE_NAME, has_permission, issue_session, resolve_session_user, verify_session
from .login import SESSION_TTL_S, hash_password
from .setup_service import STEP_IDS, SampleBody, SetupError, status, user_count, validate_public_url  # noqa: F401

PAGE = Path(__file__).with_name("ui") / "setup.html"
PROGRESS_SCRIPT = Path(__file__).with_name("ui") / "setup-progress.js"
COOKIE_PATH = "/"
SETUP_COOKIE = "aiflow4_setup_nonce"
SETUP_TTL_S = 1800
OWNERSHIP_HEADER = "x-aiflow4-ownership"


class BootstrapBody(BaseModel):
    model_config = {"extra": "forbid"}
    public_url: str = Field(min_length=1, max_length=300)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=1024)


class SkipBody(BaseModel):
    model_config = {"extra": "forbid"}
    skipped: bool


def _http_error(exc: SetupError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


def _administrator(request: Request, ctx):
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
    user = resolve_session_user(ctx.db, claims) if claims else None
    if not user:
        raise HTTPException(401, "sign in as the Administrator to continue setup")
    if not user.is_administrator or not has_permission(user.effective_roles, "integrations.manage"):
        raise HTTPException(403, "only the Administrator can change first-run setup")
    return user


def _nonce_key(ctx, nonce: str) -> str:
    return ctx.leases.p + "setup_bootstrap:" + hashlib.sha256(nonce.encode()).hexdigest()


def _bootstrap_nonce(ctx, request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(SETUP_COOKIE, "")
    if existing and ctx.leases.r.get(_nonce_key(ctx, existing)):
        return existing, False
    nonce = secrets.token_urlsafe(32)
    ctx.leases.r.set(_nonce_key(ctx, nonce), "1", ex=SETUP_TTL_S)
    return nonce, True


def _validate_bootstrap_request(request: Request, ctx) -> tuple[str, str]:
    if not _secure_setup_transport(request, ctx):
        raise HTTPException(400, "Use HTTPS to create the first Administrator from a remote browser.")
    nonce = request.cookies.get(SETUP_COOKIE, "")
    supplied_nonce = request.headers.get("x-aiflow4-setup", "")
    if (not nonce or not supplied_nonce or not secrets.compare_digest(nonce, supplied_nonce)
            or not ctx.leases.r.get(_nonce_key(ctx, nonce))):
        raise HTTPException(403, "Reload the setup screen before creating the first Administrator.")
    capability = request.headers.get(OWNERSHIP_HEADER, "")
    expected = str(getattr(ctx.settings, "setup_ownership_capability", "") or "")
    if not expected:
        raise HTTPException(503, "The installation ownership capability is not configured.")
    if not capability or not secrets.compare_digest(capability, expected):
        raise HTTPException(403, "Enter the installation ownership capability to create the first Administrator.")
    return nonce, capability


def _secure_setup_transport(request: Request, ctx) -> bool:
    """Require TLS remotely; allow direct loopback development and trusted TLS-terminating proxies."""
    return _request_is_https(request, ctx) or _loopback_client(request)


def _request_is_https(request: Request, ctx) -> bool:
    if request.url.scheme.lower() == "https":
        return True
    peer = request.client.host if request.client else ""
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for value in getattr(ctx.settings, "trusted_proxies", ()) or ():
        try:
            if peer_address in ipaddress.ip_network(value, strict=False):
                forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
                return forwarded_proto.lower() == "https"
        except ValueError:
            continue
    return False


def _loopback_client(request: Request) -> bool:
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        return False


async def _bootstrap_payload(request: Request) -> BootstrapBody:
    try:
        payload = await request.json()
        return BootstrapBody.model_validate(payload)
    except (ValueError, ValidationError, TypeError):
        raise HTTPException(422, "Enter a public address, a valid email, and a password with at least 12 characters.") from None


def setup_form_response(request: Request, ctx):
    """Serve the first-run form, or refuse it over remote plain HTTP before a secret can be typed into it."""
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if not _secure_setup_transport(request, ctx):
        return PlainTextResponse("Open this page over HTTPS to set up AI Flow. The ownership capability and the "
                                 "Administrator password must not travel over plain HTTP.", status_code=400,
                                 headers=headers)
    return FileResponse(PAGE, media_type="text/html", headers=headers)


def _register_setup_pages(app: FastAPI, ctx) -> None:
    @app.get("/setup", include_in_schema=False)
    def setup_page(request: Request):
        # The wizard screen only creates the first Administrator; later steps live on the Dashboard panel.
        if user_count(ctx):
            return RedirectResponse("/dashboard", status_code=303)
        return setup_form_response(request, ctx)

    @app.get("/ui/setup-progress.js", include_in_schema=False)
    def setup_progress_script():
        return FileResponse(PROGRESS_SCRIPT, media_type="text/javascript", headers={"Cache-Control": "no-store"})


def _register_status_route(app: FastAPI, ctx) -> None:
    @app.get("/v1/setup/status")
    def setup_status(request: Request):
        admin_exists = user_count(ctx) > 0
        if admin_exists:
            _administrator(request, ctx)
            return JSONResponse(status(ctx), headers={"Cache-Control": "no-store"})
        nonce, created = _bootstrap_nonce(ctx, request)
        data = {"admin_exists": False, "locked": False, "bootstrap_token": nonce}
        response = JSONResponse(data, headers={"Cache-Control": "no-store"})
        if created:
            response.set_cookie(SETUP_COOKIE, nonce, max_age=SETUP_TTL_S, httponly=True,
                                secure=_request_is_https(request, ctx),
                                samesite="strict", path="/v1/setup")
        return response


def _register_bootstrap_route(app: FastAPI, ctx) -> None:
    @app.post("/v1/setup/bootstrap")
    async def bootstrap(request: Request):
        nonce, _capability = _validate_bootstrap_request(request, ctx)
        if ctx.settings.console_login and len(ctx.settings.console_session_secret.encode()) < 32:
            raise HTTPException(503, "console sign-in is not configured")
        body = await _bootstrap_payload(request)
        try:
            public_url, email = setup_service.normalize_bootstrap(body.public_url, body.email)
            if not public_url.lower().startswith("https://") and not _loopback_client(request):
                raise SetupError(422, "Enter an https:// public address; plain http:// is only allowed on this machine.")
            setup_service.create_first_admin(ctx, public_url, email, hash_password(body.password))
        except SetupError as exc:
            raise _http_error(exc) from None
        ctx.leases.r.delete(_nonce_key(ctx, nonce))
        # Each API replica reads the saved value on requests through the setup middleware below.
        ctx.settings.public_url = public_url
        if not ctx.settings.join_control_url:
            ctx.settings.join_control_url = public_url
        token = issue_session(email, ["administrator"], int(time.time()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret)
        response = JSONResponse({"created": True, "email": email, "next": "/dashboard"},
                                headers={"Cache-Control": "no-store"})
        secure = public_url.lower().startswith("https://") or _request_is_https(request, ctx)
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S, httponly=True,
                            secure=secure, samesite="strict", path=COOKIE_PATH)
        response.delete_cookie(SETUP_COOKIE, path="/v1/setup", httponly=True, secure=secure, samesite="strict")
        return response


def _register_step_route(app: FastAPI, ctx) -> None:
    @app.post("/v1/setup/steps/{step_id}")
    def update_step(step_id: str, body: SkipBody, request: Request):
        user = _administrator(request, ctx)
        try:
            setup_service.set_step_skipped(ctx, step_id, body.skipped, user.subject)
        except SetupError as exc:
            raise _http_error(exc) from None
        return status(ctx)


def _register_sample_ticket_route(app: FastAPI, ctx) -> None:
    @app.post("/v1/setup/sample-ticket")
    def sample_ticket(body: SampleBody, request: Request):
        user = _administrator(request, ctx)
        try:
            return setup_service.send_sample_ticket(ctx, body, user.subject)
        except SetupError as exc:
            cause = exc.__cause__
            raise _http_error(exc) from cause


def _register_public_address_middleware(app: FastAPI, ctx) -> None:
    public_url_cache = {"read_at": 0.0}

    @app.middleware("http")
    async def load_public_address(request: Request, call_next):
        # The selected address is shared by API replicas through PostgreSQL; refresh it before OAuth and join URLs
        # are built, so the browser never needs to edit the host's environment file.
        if time.monotonic() - public_url_cache["read_at"] >= 5:
            public_url_cache["read_at"] = time.monotonic()
            try:
                url = setup_service.stored_public_url(ctx)
                if url:
                    ctx.settings.public_url = url
                    if not ctx.settings.join_control_url:
                        ctx.settings.join_control_url = url
            except Exception:  # noqa: BLE001 - database availability is handled by the route doing the work
                pass
        return await call_next(request)


def install(app: FastAPI, ctx) -> None:
    """Mount the bootstrap, setup progress and shared public-address behavior."""
    _register_setup_pages(app, ctx)
    _register_status_route(app, ctx)
    _register_bootstrap_route(app, ctx)
    _register_step_route(app, ctx)
    _register_sample_ticket_route(app, ctx)
    _register_public_address_middleware(app, ctx)
