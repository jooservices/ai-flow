"""Verify signed console sessions and define the permissions shown by the Command Center.

The console login (control/login.py, docs/v4/console-login.md) issues an HS256 JWT in the
``aiflow4_session`` cookie. A session carries a subject and the roles captured at sign-in; when a console user row
exists, its current roles, project scope and disabled state take precedence. ``ROLE_PERMISSIONS`` is the one
role-to-permission table used by both the API and the UI.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable


COOKIE_NAME = "aiflow4_session"

# View grants are permissions too: API endpoints and the console navigation check this same table.
_COMMON = {"console.read", "view.dashboard", "view.work_board"}
_VIEWS = {
    "administrator": {"*"},
    "product_owner": _COMMON | {"view.delivery", "view.business", "view.team_lead", "view.stakeholder",
                                 "view.human_review", "view.knowledge"},
    "delivery": _COMMON | {"view.delivery", "view.human_review"},
    "team_lead": _COMMON | {"view.delivery", "view.team_lead", "view.human_review", "view.knowledge"},
    "sa": _COMMON | {"view.sa", "view.machines_ai", "view.integrations", "view.knowledge"},
    "ba": _COMMON | {"view.business", "view.knowledge"},
    "business": _COMMON | {"view.business"},
    "reviewer": _COMMON | {"view.human_review"},
    "stakeholder": _COMMON | {"view.stakeholder"},
}

ROLE_LABELS = {
    "administrator": "Administrator",
    "product_owner": "Product owner",
    "delivery": "Delivery",
    "team_lead": "Team Lead",
    "sa": "SA",
    "ba": "BA",
    "business": "Business",
    "reviewer": "Reviewer",
    "stakeholder": "Stakeholder",
    # Accepted for one release; these are normalized when accounts sign in or are migrated.
    "super_admin": "Administrator",
    "owner": "Administrator",
    "lead": "Team Lead",
}

ROLE_ALIASES = {
    "super_admin": "administrator",
    "owner": "administrator",
    "lead": "team_lead",
    "Administrator": "administrator",
    "Product owner": "product_owner",
    "Delivery": "delivery",
    "Team Lead": "team_lead",
    "SA": "sa",
    "BA": "ba",
    "Business": "business",
    "Reviewer": "reviewer",
    "Stakeholder": "stakeholder",
}

# The permissions from the former table remain available to old signed sessions and old API clients.
_LEGACY_PERMISSIONS = {
    "console.manage": {"administrator"},
    "integrations.manage": {"administrator", "sa"},
    "nodes.manage": {"administrator", "sa"},
    "projects.manage": {"administrator"},
    "node.operations.read": {"administrator", "sa", "team_lead"},
    "knowledge.read": {"administrator", "product_owner", "team_lead", "sa", "ba"},
    "knowledge.manage": {"administrator"},
    "knowledge.approve": {"administrator"},
    "review.actions": {"administrator", "product_owner", "delivery", "team_lead", "reviewer"},
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {}
for _role, _views in _VIEWS.items():
    _grants = set(_views)
    if "*" not in _grants:
        for _permission, _roles in _LEGACY_PERMISSIONS.items():
            if _role in _roles:
                _grants.add(_permission)
    ROLE_PERMISSIONS[_role] = frozenset(_grants)
for _alias, _role in (("super_admin", "administrator"), ("owner", "administrator"), ("lead", "team_lead")):
    ROLE_PERMISSIONS[_alias] = ROLE_PERMISSIONS[_role]

CANONICAL_ROLES = tuple(_VIEWS)
ALLOWED_ROLES = set(ROLE_PERMISSIONS)
ROLE_PRIORITY = ("administrator", "product_owner", "delivery", "team_lead", "sa", "ba", "business",
                 "reviewer", "stakeholder")
ROLE_LANDING_PAGES = {
    "administrator": "/cc", "product_owner": "/cc", "delivery": "/delivery", "team_lead": "/lead",
    "sa": "/sa", "ba": "/business", "business": "/business", "reviewer": "/review",
    "stakeholder": "/stakeholder",
}
ROLE_VIEW_IDS = {
    "dashboard": "view.dashboard",
    "work_board": "view.work_board",
    "delivery": "view.delivery",
    "sa": "view.sa",
    "team_lead": "view.team_lead",
    "business": "view.business",
    "human_review": "view.human_review",
    "stakeholder": "view.stakeholder",
    "machines_ai": "view.machines_ai",
    "users": "console.manage",
    "knowledge": "view.knowledge",
    "integrations": "view.integrations",
    "projects": "projects.manage",
    "support": "console.manage",
    "licensing": "licensing.manage",
    "backup": "backup.read",
    "notifications": "notifications.manage",
    "security": "console.read",  # every person manages their own two-factor sign-in; administrators see the rest
}
PAGE_VIEWS = {
    "/": "dashboard", "/cc": "dashboard", "/dashboard": "dashboard",
    "/board": "work_board", "/work": "work_board", "/delivery": "delivery",
    "/sa": "sa", "/lead": "team_lead", "/business": "business", "/review": "human_review",
    "/stakeholder": "stakeholder", "/knowledge": "knowledge", "/integrations": "integrations",
    "/machines": "machines_ai", "/projects": "projects", "/users": "users",
    "/licensing": "licensing", "/backup": "backup", "/notifications": "notifications",
    "/security": "security",
    "/support": "support",
}


def normalize_role(role: str) -> str:
    """Normalize a current role label or one-release legacy role name to its stable key."""
    value = (role or "").strip()
    canonical = ROLE_ALIASES.get(value, value)
    if canonical not in _VIEWS:
        canonical = ROLE_ALIASES.get(value.casefold(), value.casefold())
    if canonical not in _VIEWS:
        raise ValueError(f"unknown role {role}; choose one of {', '.join(CANONICAL_ROLES)}")
    return canonical


def normalize_roles(roles: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize and deduplicate a set of assigned role keys or labels, preserving input order."""
    values = [roles] if isinstance(roles, str) else list(roles)
    normalized = tuple(dict.fromkeys(normalize_role(str(role)) for role in values))
    if not normalized:
        raise ValueError("a person needs at least one role")
    return normalized


def landing_page(roles: str | Iterable[str]) -> str:
    assigned = set(normalize_roles(roles))
    return ROLE_LANDING_PAGES[next(role for role in ROLE_PRIORITY if role in assigned)]


def role_permissions(roles: str | Iterable[str]) -> frozenset[str]:
    assigned = normalize_roles(roles)
    out: set[str] = set()
    for role in assigned:
        out.update(ROLE_PERMISSIONS[role])
    return frozenset(out)


def permissions_for_view(view: str) -> str:
    return ROLE_VIEW_IDS[view]


def role_satisfies(role: str | Iterable[str], required: set[str] | frozenset[str]) -> bool:
    """True when any assigned role grants a required permission identifier."""
    try:
        granted = role_permissions(role)
    except ValueError:
        granted = ROLE_PERMISSIONS.get(str(role), frozenset())
    return "*" in granted or bool(granted.intersection(required))


def has_permission(role: str | Iterable[str], permission: str) -> bool:
    return role_satisfies(role, {permission})


def resolve_session_user(db, claims: "ConsoleUser", allow_pending: bool = False) -> "ConsoleUser | None":
    """Refresh mutable account state from PostgreSQL while keeping support for pre-DB signed sessions."""
    row = db.one("SELECT role, roles, project_keys, disabled, totp_enabled FROM console_users WHERE email=%s",
                 (claims.subject,))
    if row is None:
        return claims
    if row["disabled"]:
        return None
    try:
        assigned = normalize_roles(row.get("roles") or row["role"])
    except ValueError:
        return None
    view_as = claims.view_as if claims.view_as and "administrator" in assigned else None
    projects = row.get("project_keys")
    pending = claims.mfa_pending or _needs_two_factor(row, assigned, claims)
    if pending and not allow_pending:
        return None
    return ConsoleUser(claims.subject, assigned[0], roles=assigned,
                       project_keys=tuple(projects) if projects is not None else None, view_as=view_as,
                       sid=claims.sid, mfa_pending=pending)


def _needs_two_factor(row: dict, assigned: tuple[str, ...], claims: "ConsoleUser") -> bool:
    """A session opened before the person became an administrator is limited to two-factor setup until they enrol.

    Such a session carries no pending flag, so the roles it was issued with are compared with the current ones."""
    promoted = "administrator" in assigned and "administrator" not in claims.assigned_roles
    return promoted and not row.get("totp_enabled")


def checks_bearer(endpoint):
    """Mark a route handler that verifies the Authorization bearer token itself."""
    endpoint.checks_bearer = True
    return endpoint


@dataclass(frozen=True)
class ConsoleUser:
    subject: str
    role: str
    roles: tuple[str, ...] = ()
    project_keys: tuple[str, ...] | None = None
    view_as: str | None = None
    sid: str = ""  # names the console_sessions row; empty for a token issued without one
    mfa_pending: bool = False  # signed in, but must set up two-factor sign-in before anything else

    @property
    def assigned_roles(self) -> tuple[str, ...]:
        return self.roles or (normalize_role(self.role),)

    @property
    def effective_roles(self) -> tuple[str, ...]:
        return (self.view_as,) if self.view_as else self.assigned_roles

    @property
    def permissions(self) -> frozenset[str]:
        return role_permissions(self.effective_roles)

    @property
    def is_administrator(self) -> bool:
        return "administrator" in self.assigned_roles


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# Cookies issued before sessions were tracked (JAIF-231) carry no version; they are refused so every administrator
# signs in again through the two-factor flow, and each later cookie can be ended by a forced sign-out.
SESSION_VERSION = 2

# signing secret -> callable(sid) that is True when that session was ended (forced sign-out) or is unknown.
# login.install_login registers it; verify_session asks it for every token that names a session, so every
# route that reads the cookie, not only the login gate, refuses an ended session.
SESSION_REVOKED: dict[str, Callable[[str], bool]] = {}


def csrf_token(session_cookie: str, secret: str) -> str:
    """The cross-site request token for one session: an HMAC of its cookie, so it cannot be guessed or reused."""
    return hmac.new(secret.encode(), b"csrf:" + session_cookie.encode(), hashlib.sha256).hexdigest()[:40]


def issue_session(subject: str, role: str | Iterable[str], expires_at: int, secret: str,
                  view_as: str | None = None, sid: str = "", mfa_pending: bool = False) -> str:
    """Create the token that the console authentication flow sets as its HttpOnly cookie."""
    try:
        roles = normalize_roles(role)
        acting_as = normalize_role(view_as) if view_as else None
    except ValueError:
        raise ValueError("invalid console session claims or signing secret") from None
    if not subject.strip() or len(secret.encode()) < 32 or (acting_as and "administrator" not in roles):
        raise ValueError("invalid console session claims or signing secret")
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims: dict = {"sub": subject.strip(), "role": roles[0], "roles": list(roles), "exp": int(expires_at),
                    "v": SESSION_VERSION, **({"view_as": acting_as} if acting_as else {})}
    if sid:
        claims["sid"] = sid
    if mfa_pending:
        claims["pm"] = 1
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    message = f"{header}.{payload}"
    signature = hmac.new(secret.encode(), message.encode("ascii"), hashlib.sha256).digest()
    return f"{message}.{_b64(signature)}"


def verify_session(token: str, secret: str, now: int | None = None, allow_pending: bool = False) -> ConsoleUser | None:
    """Return claims only for a valid, unexpired, not ended session signed with the configured key.

    A session that still has to set up two-factor sign-in is refused unless `allow_pending` (only the two-factor
    setup routes pass it)."""
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
        roles_claim = claims.get("roles", [role])
        if not isinstance(roles_claim, list) or not all(isinstance(item, str) for item in roles_claim):
            return None
        roles = normalize_roles(roles_claim)
        if not isinstance(expires_at, int) or expires_at <= (int(time.time()) if now is None else now):
            return None
        if claims.get("v") != SESSION_VERSION:  # a cookie from before the hardening: no two-factor, no forced sign-out
            return None
        sid = claims.get("sid", "")
        if not isinstance(sid, str):
            return None
        pending = claims.get("pm") == 1
        if pending and not allow_pending:
            return None
        revoked = SESSION_REVOKED.get(secret)
        if sid and revoked and revoked(sid):
            return None
        view_as_value = claims.get("view_as")
        view_as = normalize_role(view_as_value) if isinstance(view_as_value, str) else None
        if view_as and "administrator" not in roles:
            return None
        return ConsoleUser(subject.strip(), roles[0], roles=roles, view_as=view_as, sid=sid, mfa_pending=pending)
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
