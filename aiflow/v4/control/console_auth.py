"""Verify signed console sessions and define the permissions shown by the Command Center.

The console login (control/login.py, docs/v4/console-login.md) issues this small HS256 JWT contract in the
``aiflow4_session`` cookie: ``sub`` is the authenticated user, ``role`` is one of ``ROLE_PERMISSIONS``, and ``exp``
is the Unix expiry time. We never trust identity or role headers from the request.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


COOKIE_NAME = "aiflow4_session"
# Role -> permissions. "*" is every permission. Call sites ask for permission identifiers, so a new role can be
# given access by adding its permissions here without changing endpoint code.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "super_admin": frozenset({"*"}),
    "owner": frozenset({
        "console.read", "console.write", "console.manage", "integrations.manage", "nodes.manage", "projects.manage",
        "node.operations.read", "knowledge.read", "knowledge.manage", "knowledge.approve", "notifications.manage",
    }),
    "lead": frozenset({"console.read", "node.operations.read", "knowledge.read"}),
}
ROLE_LABELS = {"super_admin": "Super Administrator", "owner": "Owner", "lead": "Lead"}
ALLOWED_ROLES = set(ROLE_PERMISSIONS)


def role_satisfies(role: str, required: set[str] | frozenset[str]) -> bool:
    """True when the role has at least one required permission identifier."""
    granted = ROLE_PERMISSIONS.get(role, frozenset())
    return "*" in granted or bool(granted.intersection(required))


def has_permission(role: str, permission: str) -> bool:
    granted = ROLE_PERMISSIONS.get(role, frozenset())
    return "*" in granted or permission in granted


def checks_bearer(endpoint):
    """Mark a route handler that verifies the `Authorization` bearer token itself (not through a dependency), so
    the console login gate (login.py) lets bearer requests reach it; the handler still refuses a wrong token."""
    endpoint.checks_bearer = True
    return endpoint


@dataclass(frozen=True)
class ConsoleUser:
    subject: str
    role: str

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(subject: str, role: str, expires_at: int, secret: str) -> str:
    """Create the token that the console authentication flow sets as its HttpOnly cookie."""
    if not subject.strip() or role not in ALLOWED_ROLES or len(secret.encode()) < 32:
        raise ValueError("invalid console session claims or signing secret")
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({"sub": subject.strip(), "role": role, "exp": int(expires_at)},
                              separators=(",", ":")).encode())
    message = f"{header}.{payload}"
    signature = hmac.new(secret.encode(), message.encode("ascii"), hashlib.sha256).digest()
    return f"{message}.{_b64(signature)}"


def verify_session(token: str, secret: str, now: int | None = None) -> ConsoleUser | None:
    """Return claims only for a valid, unexpired session signed with the configured key."""
    if not token or len(secret.encode()) < 32:
        return None
    try:
        header, payload, signature = token.split(".")
        message = f"{header}.{payload}"
        expected = hmac.new(secret.encode(), message.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature)):
            return None
        header_data = json.loads(_unb64(header))
        claims = json.loads(_unb64(payload))
        if not isinstance(header_data, dict) or not isinstance(claims, dict):
            return None
        if header_data != {"alg": "HS256", "typ": "JWT"}:
            return None
        subject, role, expires_at = claims.get("sub"), claims.get("role"), claims.get("exp")
        if not isinstance(subject, str) or not subject.strip() or role not in ALLOWED_ROLES:
            return None
        if not isinstance(expires_at, int) or expires_at <= (int(time.time()) if now is None else now):
            return None
        return ConsoleUser(subject.strip(), role)
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
