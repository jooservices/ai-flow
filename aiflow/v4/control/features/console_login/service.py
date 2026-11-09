"""Console sign-in, sessions and user management use cases (no FastAPI: routes turn the results into responses).

Sign-in order and the rules behind it are described in `control/login.py` and `docs/v4/security.md`."""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from functools import partial

from ... import audit as audit_log, console_auth, licensing, login, mfa
from ...console_auth import ROLE_LABELS, landing_page, normalize_roles, resolve_session_user, verify_session
from ...emails import normalize_email
from ...login import (LOCK_S, MAX_FAILURES, RATE_WINDOW_S, REFUSED, _dummy_hash, mfa_role, require_mfa, seed_user,
                      session_ttl_s)
from . import repository

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptState:
    row: object | None
    locked: bool
    enrolled: bool
    second_factor: bool
    usable: bool
    good: bool
    factor: str


# --- sign-in --------------------------------------------------------------------------------------------------------
def rate_limit_key(prefix: str, *parts: str) -> str:
    return prefix + "console_login:" + ":".join(parts)


def rate_limit_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def audit_console_event(ctx, action: str, email: str, accepted: bool, result: str) -> None:
    repository.write_console_audit(ctx.db, action, email, accepted, result)


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
    repository.clear_expired_lock(ctx.db, email)
    return repository.find_user(ctx.db, email)


def limited_signin_response(limited: bool, usable: bool, good: bool, enrolled: bool, factor: str,
                            wait: int) -> tuple[int, dict, dict] | None:
    if limited and not (usable and good and enrolled and factor):
        return 429, {"detail": "too many sign-in attempts; try again later"}, {"Retry-After": str(wait)}
    return None


def failed_signin_response(ctx, email: str, usable: bool, locked: bool, second_factor: bool):
    if usable and not locked:
        repository.record_failed_attempt(ctx.db, email, MAX_FAILURES, LOCK_S)
    audit_failed_signin(ctx, email, "account locked" if locked and not second_factor else REFUSED)
    return 401, {"detail": REFUSED}, {}


def successful_signin(ctx, settings, ip: str, email: str, row, factor: str):
    repository.record_signed_in(ctx.db, email)
    ctx.leases.r.delete(rate_limit_key(ctx.leases.p, "ip_email",
                                       rate_limit_digest(f"{ip}\n{email}")))
    audit_console_event(ctx, "login", email, True, "signed in")
    roles = row.get("roles") or [row["role"]]
    pending = mfa_role(roles) and not row["totp_enabled"] and require_mfa(ctx)
    ttl = session_ttl_s(ctx)
    sid = secrets.token_urlsafe(18)
    repository.open_session(ctx.db, sid, email, row["role"], ip, ttl)
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


# --- sessions -------------------------------------------------------------------------------------------------------
def user_json(email: str, roles: list[str] | tuple[str, ...], name: str = "") -> dict:
    assigned = normalize_roles(roles)
    return {"email": email, "name": name, "role": assigned[0], "role_label": ROLE_LABELS[assigned[0]],
            "roles": list(assigned), "role_labels": [ROLE_LABELS[role] for role in assigned],
            "landing_page": landing_page(assigned)}


def end_session(ctx, settings, session_cookie: str) -> str:
    """Mark the session named by the cookie as ended; return who it belonged to."""
    user = verify_session(session_cookie, settings.console_session_secret, allow_pending=True)
    if user and user.sid:
        repository.end_session(ctx.db, user.sid, user.subject)
        audit_log.record(ctx.db, user.subject, "auth", "signed_out", user.subject)
    return user.subject if user else ""


def session_revoked(ctx, sid: str) -> bool:
    """Fail closed when a session was ended, expired, unknown, or cannot be checked."""
    try:
        return not repository.session_is_live(ctx.db, sid)
    except Exception:  # noqa: BLE001 - fail closed when the session database cannot be checked
        log.exception("console session check failed")
        return True


def register_session_revocation(ctx) -> None:
    secret = ctx.settings.console_session_secret
    if len(secret.encode()) >= 32:
        console_auth.SESSION_REVOKED[secret] = partial(session_revoked, ctx)


def current_user(ctx, settings, session_cookie: str) -> dict | None:
    """The signed-in user as the console reads it (also a session that still owes its second factor), or None."""
    claims = verify_session(session_cookie, settings.console_session_secret, allow_pending=True)
    user = resolve_session_user(ctx.db, claims, True) if claims else None
    if not user:
        return None
    data = user_json(user.subject, list(user.assigned_roles), repository.display_name(ctx.db, user.subject))
    data["landing_page"] = landing_page(user.effective_roles)
    data["mfa_setup_required"] = user.mfa_pending
    return data


# --- users ----------------------------------------------------------------------------------------------------------
def list_console_users(ctx, settings) -> dict:
    rows = repository.list_users(ctx.db)
    state = licensing.snapshot(ctx.db, settings.license_public_key)
    count = repository.count_active_users(ctx.db)
    users = [user_json(row["email"], row["role"], row["display_name"])
             | {"disabled": row["disabled"], "created_at": row["created_at"].isoformat()} for row in rows]
    return {"users": users, "count": count, "limit": state["limits"]["users"]}


def valid_new_user(data) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("email"), str) and isinstance(data.get("role"), str))


def create_console_user(ctx, settings, actor: str, data, ip: str) -> tuple[int, dict]:
    """Create a user with a generated password; return the status and the body of the response."""
    if not valid_new_user(data):
        return 422, {"detail": "Enter an email address and choose a role."}
    display_name = data.get("display_name", "")
    if not isinstance(display_name, str) or len(display_name) > 120:
        return 422, {"detail": "Display name must be 120 characters or fewer."}
    try:
        email = normalize_email(data["email"])
        password = seed_user(ctx.db, email, data["role"], display_name, public_key=settings.license_public_key)
    except ValueError as exc:
        if isinstance(exc, licensing.LicenseError):
            status = 423 if "read-only" in str(exc).lower() else 409
        else:
            status = 422
        return status, {"detail": str(exc)}
    if password is None:
        return 409, {"detail": "A console user with this email already exists."}
    audit_console_event(ctx, "create_user", email, True, "created")
    audit_log.record(ctx.db, actor, "people", "person_added", email, {"roles": [data["role"]]}, ip)
    return 201, {"email": email, "name": display_name.strip(), "role": data["role"], "initial_password": password,
                 "message": "Account created. Copy the initial password and share it securely."}
