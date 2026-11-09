"""The session and CSRF cookies of the console, and the session length limits."""
from __future__ import annotations

from ...console_auth import COOKIE_NAME, csrf_token

CSRF_COOKIE = "aiflow4_csrf"
SESSION_TTL_S = 12 * 3600
SESSION_TTL_MIN_S, SESSION_TTL_MAX_S = 5 * 60, 30 * 24 * 3600


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
