"""Console login: people sign in with an email and a password and get the signed `aiflow4_session` cookie.

`console_auth.py` defines the cookie and the roles; this module adds the users (table `console_users`), the
password hashing (scrypt from the standard library, salted, no new dependency), the sign-in page, the endpoints
and the gate. The gate only works with `AIFLOW4_CONSOLE_LOGIN=1`; without it nothing changes.

Sign-in, in order:
1. Redis rate limits per client IP and per IP+email; when those budgets are spent, only a valid password plus a
   one-time second factor can still sign in (429 with Retry-After for other attempts).
2. The user row; an account whose lock has expired gets `failed_attempts` reset first.
3. scrypt verification in a worker thread, never on the event loop. Unknown emails and locked accounts are checked
   against a dummy hash, so every refusal costs the same and returns the same 401 answer.
4. A wrong password counts a failure; MAX_FAILURES in a row lock the account for LOCK_S.
Failed sign-ins are audited, but at most AUDIT_FAILED_PER_MIN rows a minute, so a flood cannot grow the table.

Two-factor (docs/v4/security.md, mfa.py): an enrolled person adds `code` (authenticator code or recovery code). The lock of
step 4 stops password-only guessing but not a request that also carries a valid second factor, so the real owner is never
locked out. Each sign-in creates a `console_sessions` row named by the cookie's `sid`; ending that row (forced sign-out,
logout, password reset) makes every later request on the cookie fail (console_auth.SESSION_REVOKED). Cookies: the session
cookie is HttpOnly; the `aiflow4_csrf` cookie carries the CSRF token that security.py requires on state-changing requests.

Gate rules when it is on:
- a console page without a valid session redirects to `/login?next=...`; any other path answers 401;
- a request that carries an `Authorization` header passes only to routes that check a bearer token themselves:
  a dependency or handler with an `authorization` header parameter (nodes, workers, reviewer, owner, release files,
  `/v1/policy/decide`) or a handler marked with `console_auth.checks_bearer` (the owner-token Jira routes). An
  attacker could add the header to any request, so other routes still need the session;
- `/healthz`, `/login`, `/logout`, `/v1/auth/*`, the node join/install endpoints (join grants carry their own one-use
  token) and the Jira and GitHub consent callbacks (checked by their one-use state) stay open.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import stat
import time
from typing import Callable
from urllib.parse import quote, urlsplit

import redis
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from .application_context import ApplicationContext
from starlette.routing import Match

from . import audit as audit_log
from . import console_auth, mfa
from .console_auth import (COOKIE_NAME, PAGE_VIEWS, ROLE_LABELS, ROLE_PERMISSIONS, ConsoleUser, csrf_token,
                           has_permission, issue_session, landing_page, normalize_roles, permissions_for_view,
                           resolve_session_user, verify_session)
from .db import J
from .emails import EMAIL, normalize_email  # noqa: F401 - re-exported for callers of login.normalize_email
from .ui import CONSOLE_PAGE_PATHS

log = logging.getLogger(__name__)

CSRF_COOKIE = "aiflow4_csrf"  # readable by the console script, which echoes it in the X-CSRF-Token header
SESSION_TTL_S = 12 * 3600  # default session length; the "session length" security setting overrides it
SESSION_TTL_MIN_S, SESSION_TTL_MAX_S = 5 * 60, 30 * 24 * 3600
MFA_ROLES = ("administrator",)  # two-factor sign-in is mandatory for anyone holding one of these roles


def mfa_role(roles) -> bool:
    """True when the assigned role (or any of several) is one that must sign in with two factors."""
    try:
        return any(role in MFA_ROLES for role in normalize_roles(roles))
    except ValueError:
        return False
MAX_FAILURES = 5          # wrong passwords in a row before the account is locked
LOCK_S = 15 * 60
RATE_WINDOW_S = 15 * 60
RATE_PER_IP = 30          # sign-in attempts per client IP per window
RATE_PER_IP_EMAIL = 10    # sign-in attempts per client IP and email per window
AUDIT_FAILED_PER_MIN = 30
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
REFUSED = "invalid email or password"

PAGES = frozenset(CONSOLE_PAGE_PATHS) | frozenset(PAGE_VIEWS) | {"/setup"}
OPEN_PATHS = {"/login", "/logout", "/healthz", "/v1/install.sh", "/v1/nodes/join", "/v1/nodes/join-tokens",
              "/v1/nodes/join-tokens/inspect",
              # provider redirects arrive from another site without the Strict cookie; a one-use state protects them
              "/v1/integrations/jira/oauth/callback", "/v1/integrations/github/manifest/callback",
              "/v1/integrations/github/setup",
              # GitHub calls it without a session; the HMAC signature of the configured secret is the credential
              "/v1/github/webhook"}
OPEN_PREFIXES = ("/v1/auth/",)


# --- passwords ---------------------------------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def generate_password() -> str:
    """32 URL-safe characters (192 bits)."""
    return secrets.token_urlsafe(24)


_DUMMY_HASH = ""


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if not _DUMMY_HASH:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


# --- users -------------------------------------------------------------------------------------------------------
def write_password_file(path: str, password: str) -> None:
    """Write `password` to `path`, private from the first byte: the final component may not be a symlink, an existing
    file must be a regular file of this user with one link, and mode 0600 is set on the open descriptor."""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as e:
        raise ValueError(f"cannot open the password file {path}: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            raise ValueError(f"refusing the password file {path}: not a private regular file of this user")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        data = (password + "\n").encode()
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


def seed_user(db, email: str, role: str | list[str] | tuple[str, ...], display_name: str = "",
              deliver: Callable[[str], None] | None = None, public_key: str = "") -> str | None:
    """Create the user with a generated password and return it; return None when the user already exists.

    `deliver(password)` runs inside the transaction, before the commit: when it fails the user is rolled back, so a
    password nobody received never becomes the account's password."""
    email = normalize_email(email)
    roles = normalize_roles(role)
    from . import licensing
    password = generate_password()
    password_hash = hash_password(password)
    with db.pool.connection() as c:
        with c.transaction():
            licensing.lock_resource_admission(c)
            if c.execute("SELECT 1 AS x FROM console_users WHERE email=%s", (email,)).fetchone():
                return None
            count = int(c.execute("SELECT count(*) AS count FROM console_users WHERE disabled=false").fetchone()["count"])
            licensing.require_count_capacity(db, "users", count, public_key=public_key, connection=c)
            created = c.execute("INSERT INTO console_users (email, display_name, role, roles, password_hash)"
                                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING RETURNING email",
                                (email, display_name.strip(), roles[0], list(roles), password_hash)).fetchone()
            if not created:
                return None
            audited = [("people", "user_created")] + ([("roles", "role_granted")] if role != "lead" else [])
            for category, action in audited:  # same transaction as the insert, so no user exists without its entry
                c.execute("INSERT INTO audit_log (actor, category, action, target, ip, detail) VALUES (%s,%s,%s,%s,%s,%s)",
                          ("cli", category, action, email[:300], "", J({"role": role})))
            if deliver:
                deliver(password)
    return password


def reset_password(db, email: str, deliver: Callable[[str], None] | None = None, public_key: str = "") -> str:
    """Give an existing user a new generated password and clear any lock; `deliver` runs before the commit."""
    from . import licensing
    try:
        licensing.require_writable(db, public_key)
    except licensing.LicenseError as exc:
        raise ValueError(str(exc)) from None
    email = normalize_email(email)
    password = generate_password()
    with db.transaction() as c:
        updated = c.execute("UPDATE console_users SET password_hash=%s, failed_attempts=0, locked_until=NULL,"
                            " updated_at=now() WHERE email=%s RETURNING email", (hash_password(password), email)).fetchone()
        if not updated:
            raise ValueError("no such console user")
        c.execute("UPDATE console_sessions SET revoked_at=now(), revoked_by='password reset'"
                  " WHERE email=%s AND revoked_at IS NULL", (email,))  # a new password ends every open session
        audit_log.record(db, "cli", "people", "password_reset", email)
        if deliver:
            deliver(password)
    return password


def get_setting(db, key: str, default: str) -> str:
    row = db.one("SELECT value FROM security_settings WHERE key=%s", (key,))
    return row["value"] if row else default


def set_setting(db, key: str, value: str, actor: str) -> None:
    db.run("INSERT INTO security_settings (key, value, updated_by) VALUES (%s,%s,%s)"
           " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, updated_at=now()",
           (key, value, actor))


def session_ttl_s(ctx) -> int:
    try:
        value = int(get_setting(ctx.db, "session_ttl_s", str(SESSION_TTL_S)))
    except ValueError:
        value = SESSION_TTL_S
    return max(SESSION_TTL_MIN_S, min(SESSION_TTL_MAX_S, value))


def require_mfa(_ctx) -> bool:
    """Administrator MFA is mandatory even when a legacy setting says otherwise."""
    return True


# --- HTTP ----------------------------------------------------------------------------------------------------------
def safe_next(value: str) -> str:
    """Only a path on this site: no scheme, no host, no protocol-relative URL."""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value:
        return value
    return "/cc"


def client_ip(request: Request, trusted_proxies: list[str] | tuple[str, ...] = ()) -> str:
    """Use forwarding headers only when the socket peer belongs to a configured proxy CIDR.

    Direct-origin callers are keyed by their actual peer address, even if they send forged Cloudflare or
    X-Forwarded-For headers. Proxies must sanitize the forwarded values they provide.
    """
    peer = request.client.host if request.client else "unknown"
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer[:64]

    trusted = False
    for value in trusted_proxies:
        try:
            if peer_address in ipaddress.ip_network(value, strict=False):
                trusted = True
                break
        except ValueError:
            # A malformed proxy entry cannot expand the trust boundary.
            continue
    if not trusted:
        return str(peer_address)

    for value in (request.headers.get("cf-connecting-ip", ""),
                  request.headers.get("x-forwarded-for", "").split(",", 1)[0]):
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError:
            continue
    return str(peer_address)


def _origin_tuple(value: str, *, referrer: bool = False) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        scheme, host = parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip(".")
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        if not referrer and (parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, host, port
    except ValueError:
        return None


def preauth_origin_allowed(request: Request, public_url: str) -> bool:
    """Require a same-origin marker on the pre-authentication login request."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    origin = request.headers.get("origin", "")
    referrer = request.headers.get("referer", "")
    supplied = origin or referrer
    if not supplied:
        return False
    source = _origin_tuple(supplied, referrer=not bool(origin))
    if source is None:
        return False
    expected = {_origin_tuple(str(request.base_url)), _origin_tuple(public_url)}
    expected.discard(None)
    return source in expected


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · AI Flow</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#14171c;--muted:#5d6672;--line:#d9dde3;--accent:#2557d6;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--text:#e8ebef;--muted:#9aa4b0;--line:#2a313a;--accent:#7aa2ff;--err:#ff8b84}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:16px}
main{width:100%;display:grid;place-items:center}
form{width:100%;max-width:360px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 20px;color:var(--muted);font-size:13px}
label{display:block;font-size:13px;margin:12px 0 4px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--text);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:20px;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:default}#msg{min-height:20px;margin-top:12px;color:var(--err);font-size:13px}
</style></head><body>
<main><form id="f" autocomplete="on"><h1>AI Flow Command Center</h1><p>Sign in to continue.</p>
<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required autofocus>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<label for="code">Authentication code or recovery code <span style="color:var(--muted)">(if two-factor is on)</span></label>
<input id="code" name="code" type="text" autocomplete="one-time-code" inputmode="text" maxlength="32">
<button id="go" type="submit">Sign in</button><div id="msg" role="alert"></div></form></main>
<script>
var next=new URLSearchParams(location.search).get("next")||"/cc";
if(next.charAt(0)!=="/"||next.charAt(1)==="/"||next.indexOf("\\\\")>=0){next="/cc"}
// SameSite=Strict: a page reached from another site arrives without the cookie; this same-site request has it.
fetch("/v1/auth/me",{credentials:"same-origin"}).then(function(r){if(r.status===200){return r.json()}
}).then(function(d){if(d){location.replace(d.landing_page||next)}});
document.getElementById("f").addEventListener("submit",function(e){e.preventDefault();
var b=document.getElementById("go"),m=document.getElementById("msg");b.disabled=true;m.textContent="";
fetch("/v1/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",
body:JSON.stringify({email:document.getElementById("email").value,password:document.getElementById("password").value,code:document.getElementById("code").value})})
.then(function(r){return r.json().then(function(d){return[r.status,d]})})
.then(function(x){if(x[0]===200){location.href=x[1].mfa_setup_required?"/security":(x[1].landing_page||next)}
else{m.textContent=x[1].detail||"Sign-in failed";b.disabled=false;if(x[1].mfa_required){document.getElementById("code").focus()}}})
.catch(function(){m.textContent="Cannot reach the server";b.disabled=false})});
</script></body></html>
"""


def install_login(app: FastAPI, ctx: ApplicationContext) -> None:
    from . import setup_wizard
    from .route_registry import concrete_routes
    from .security import install_security

    settings = ctx.settings
    token_routes: dict[int, bool] = {}

    def flatten(dependant):
        yield dependant
        for sub in dependant.dependencies:
            yield from flatten(sub)

    def route_checks_token(route: APIRoute) -> bool:
        if getattr(route.endpoint, "checks_bearer", False):
            return True
        return any(param.name == "authorization" for d in flatten(route.dependant) for param in d.header_params)

    def resolved_route(route, scope):
        """Return the matching leaf route when FastAPI wraps included routers in the app route table."""
        if isinstance(route, APIRoute):
            return route
        matcher = getattr(route, "_match", None)
        if callable(matcher):
            result = matcher(scope)
            if result[0] == Match.FULL and len(result) > 2:
                return resolved_route(result[2], scope)
        return None

    def checks_token(request: Request) -> bool:
        for route in concrete_routes(app.routes):
            if route.matches(request.scope)[0] == Match.FULL:
                matched = resolved_route(route, request.scope)
                if matched is None:
                    return False
                if id(matched) not in token_routes:
                    token_routes[id(matched)] = route_checks_token(matched)
                return token_routes[id(matched)]
        return False

    def key(*parts: str) -> str:
        return ctx.leases.p + "console_login:" + ":".join(parts)

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:32]

    def over_limit(ip: str, email: str) -> int:
        """Count this attempt; return the seconds to wait when the IP or IP+email budget is spent, else 0."""
        r = ctx.leases.r
        keys = ((key("ip", digest(ip)), RATE_PER_IP), (key("ip_email", digest(f"{ip}\n{email}")), RATE_PER_IP_EMAIL))
        pipe = r.pipeline()
        for k, _ in keys:
            pipe.set(k, 0, ex=RATE_WINDOW_S, nx=True)  # the window starts at the first attempt
            pipe.incr(k)
        counts = pipe.execute()[1::2]
        for (k, limit), count in zip(keys, counts):
            if int(count) > limit:
                return max(1, int(r.ttl(k)))
        return 0

    def audit(action: str, email: str, accepted: bool, result: str) -> None:
        ctx.db.run("INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", ("console", action, "", "", None, email, accepted, result, J({})))

    def audit_failure(email: str, result: str) -> None:
        bucket = key("audit", str(int(time.time()) // 60))
        pipe = ctx.leases.r.pipeline()
        pipe.incr(bucket)
        pipe.expire(bucket, 120)
        count = int(pipe.execute()[0])
        if count <= AUDIT_FAILED_PER_MIN:
            audit("login", email, False, result)
        elif count == AUDIT_FAILED_PER_MIN + 1:
            log.warning("console sign-in: more than %s failures this minute; further failures are not audited",
                        AUDIT_FAILED_PER_MIN)

    def secure_cookie() -> bool:
        return str(settings.public_url or "").lower().startswith("https://")

    def user_json(email: str, roles: list[str] | tuple[str, ...], name: str = "") -> dict:
        assigned = normalize_roles(roles)
        return {"email": email, "name": name, "role": assigned[0], "role_label": ROLE_LABELS[assigned[0]],
                "roles": list(assigned), "role_labels": [ROLE_LABELS[role] for role in assigned],
                "landing_page": landing_page(assigned)}

    def session_revoked(sid: str) -> bool:
        """True when the session was ended (forced sign-out, logout, password reset), expired, or is unknown.
        Fails closed: when the database cannot answer, the session does not count."""
        try:
            return ctx.db.one("SELECT 1 AS x FROM console_sessions WHERE sid=%s AND revoked_at IS NULL"
                              " AND expires_at > now()", (sid,)) is None
        except Exception:  # noqa: BLE001
            log.exception("console session check failed")
            return True

    def register_revocation() -> None:
        if len(settings.console_session_secret.encode()) >= 32:
            console_auth.SESSION_REVOKED[settings.console_session_secret] = session_revoked

    register_revocation()

    def attempt(ip: str, email: str, password: str, code: str = "") -> tuple[int, dict, dict]:
        """The blocking part of a sign-in (Redis, database, scrypt); runs in a worker thread.

        Password guessing is slowed and blocked: per-IP and per-IP+email budgets, and MAX_FAILURES wrong passwords
        lock the account against password-only attempts. The lock never shuts the real owner out: someone who also
        presents a valid authenticator code or an unused recovery code (a second factor a guesser does not have) is
        still checked and signed in, and success clears the lock."""
        wait = over_limit(ip, email)
        limited = bool(wait)
        with ctx.db.transaction():
            return check_attempt(ip, email, password, code, wait, limited)

    def check_attempt(ip: str, email: str, password: str, code: str, wait: int, limited: bool):
        row = None
        if email:
            ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, updated_at=now()"
                       " WHERE email=%s AND locked_until IS NOT NULL AND locked_until <= now()", (email,))
            row = ctx.db.one("SELECT email, display_name, role, roles, password_hash, disabled, locked_until,"
                             " totp_secret, totp_enabled FROM console_users WHERE email=%s", (email,))
        locked = bool(row and row["locked_until"])
        enrolled = bool(row and row["totp_enabled"])
        second_factor = bool(code.strip()) and enrolled
        usable = bool(row and not row["disabled"] and (not locked or second_factor))
        good = verify_password(password, row["password_hash"] if usable else _dummy_hash())
        factor = ""
        if usable and good and enrolled and code.strip():
            factor = mfa.check_second_factor(ctx.db, row, email, code, settings.console_session_secret)
            good = bool(factor)
        if limited and not (usable and good and enrolled and factor):
            return 429, {"detail": "too many sign-in attempts; try again later"}, {"Retry-After": str(wait)}
        if usable and good and enrolled and not code.strip():
            # right password, second factor still to come; not a failure and nothing is revealed to a wrong password
            return 401, {"detail": "enter the code from your authenticator app or a recovery code",
                         "mfa_required": True}, {}
        if not (usable and good):
            if usable and not locked:
                ctx.db.run("UPDATE console_users SET failed_attempts=failed_attempts+1,"
                           " locked_until=CASE WHEN failed_attempts+1 >= %s THEN now() + make_interval(secs => %s)"
                           " ELSE locked_until END, updated_at=now() WHERE email=%s", (MAX_FAILURES, LOCK_S, email))
            audit_failure(email, "account locked" if locked and not second_factor else REFUSED)
            return 401, {"detail": REFUSED}, {}
        ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, last_login_at=now(), updated_at=now()"
                   " WHERE email=%s", (email,))
        ctx.leases.r.delete(key("ip_email", digest(f"{ip}\n{email}")))
        audit("login", email, True, "signed in")
        roles = row.get("roles") or [row["role"]]
        pending = mfa_role(roles) and not enrolled and require_mfa(ctx)
        ttl = session_ttl_s(ctx)
        sid = secrets.token_urlsafe(18)
        ctx.db.run("INSERT INTO console_sessions (sid, email, role, ip, expires_at)"
                   " VALUES (%s,%s,%s,%s, now() + make_interval(secs => %s))", (sid, email, row["role"], ip, ttl))
        audit_log.record(ctx.db, email, "auth", "signed_in", email,
                         {"second_factor": factor or "none", "mfa_setup_required": pending}, ip)
        body = user_json(email, roles, row["display_name"])
        body.update({"mfa_setup_required": pending, "_sid": sid, "_ttl": ttl})
        if factor == "recovery":
            body["recovery_codes_left"] = mfa.recovery_codes_left(ctx.db, email)
        return 200, body, {}

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> HTMLResponse:
        if setup_wizard.user_count(ctx) == 0:
            return setup_wizard.setup_form_response(request, ctx)
        return HTMLResponse(LOGIN_PAGE, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post("/v1/auth/login")
    async def login(request: Request) -> JSONResponse:
        secret = settings.console_session_secret
        if len(secret.encode()) < 32:
            return JSONResponse({"detail": "console sign-in is not configured"}, status_code=503)
        if not preauth_origin_allowed(request, settings.public_url):
            return JSONResponse({"detail": "cross-site sign-in request refused"}, status_code=403)
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
            return JSONResponse({"detail": "enter your email and password"}, status_code=400)
        try:
            email = normalize_email(data["email"])
        except ValueError:
            email = ""
        try:
            code = data.get("code") if isinstance(data.get("code"), str) else ""
            status, body, headers = await run_in_threadpool(
                attempt, client_ip(request, settings.trusted_proxies), email, data["password"][:1024], code[:64])
        except redis.RedisError:
            log.exception("console sign-in: Redis is unavailable")
            return JSONResponse({"detail": "sign-in is unavailable; try again shortly"}, status_code=503)
        sid, ttl = body.pop("_sid", ""), body.pop("_ttl", SESSION_TTL_S)
        response = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **headers})
        if status == 200:
            register_revocation()
            token = issue_session(email, body["roles"], int(time.time()) + ttl, secret, sid=sid,
                                  mfa_pending=body["mfa_setup_required"])
            set_session_cookies(response, token, ttl)
        return response

    def set_session_cookies(response, token: str, ttl: int) -> None:
        response.set_cookie(COOKIE_NAME, token, max_age=ttl, httponly=True, secure=secure_cookie(), samesite="strict", path="/")
        # The console script reads this cookie and echoes it in X-CSRF-Token; it only works with this session's cookie.
        response.set_cookie(CSRF_COOKIE, csrf_token(token, settings.console_session_secret), max_age=ttl, httponly=False,
                            secure=secure_cookie(), samesite="strict", path="/")

    def end_session(request: Request, actor_hint: str = "") -> str:
        """Mark the session named by the request's cookie as ended; returns who it belonged to."""
        user = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret, allow_pending=True)
        if user and user.sid:
            ctx.db.run("UPDATE console_sessions SET revoked_at=now(), revoked_by=%s WHERE sid=%s AND revoked_at IS NULL",
                       (user.subject, user.sid))
            audit_log.record(ctx.db, user.subject, "auth", "signed_out", user.subject)
        return user.subject if user else ""

    def clear_cookies(response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=secure_cookie(), samesite="strict")
        response.delete_cookie(CSRF_COOKIE, path="/", secure=secure_cookie(), samesite="strict")

    @app.post("/v1/auth/logout")
    def logout(request: Request) -> JSONResponse:
        end_session(request)
        response = JSONResponse({"signed_out": True})
        clear_cookies(response)
        return response

    @app.post("/logout", include_in_schema=False)
    def logout_page(request: Request) -> RedirectResponse:
        end_session(request)
        response = RedirectResponse("/login", status_code=303)
        clear_cookies(response)
        return response

    @app.get("/v1/auth/me")
    def me(request: Request) -> JSONResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret,
                                allow_pending=True)
        user = resolve_session_user(ctx.db, claims, True) if claims else None
        if not user:
            return JSONResponse({"detail": "not signed in"}, status_code=401)
        row = ctx.db.one("SELECT display_name FROM console_users WHERE email=%s", (user.subject,))
        data = user_json(user.subject, list(user.assigned_roles), (row or {}).get("display_name", ""))
        data["landing_page"] = landing_page(user.effective_roles)
        data["mfa_setup_required"] = user.mfa_pending
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    app.state.set_session_cookies = set_session_cookies  # the two-factor setup route issues a full session after enrolling

    def users_manager(request: Request):
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user:
            return JSONResponse({"detail": "authenticated console session required"}, status_code=401)
        if not has_permission(user.effective_roles, "users.manage"):
            return JSONResponse({"detail": "owner role required"}, status_code=403)
        return user

    @app.get("/v1/auth/users")
    def console_users(request: Request) -> JSONResponse:
        user = users_manager(request)
        if isinstance(user, JSONResponse):
            return user
        from . import licensing
        rows = ctx.db.all("SELECT email,display_name,role,disabled,created_at FROM console_users ORDER BY email")
        state = licensing.snapshot(ctx.db, settings.license_public_key)
        count = int(ctx.db.one("SELECT count(*) AS count FROM console_users WHERE disabled=false")["count"])
        return JSONResponse({"users": [user_json(row["email"], row["role"], row["display_name"])
                                       | {"disabled": row["disabled"], "created_at": row["created_at"].isoformat()}
                                       for row in rows],
                             "count": count,
                             "limit": state["limits"]["users"]})

    @app.post("/v1/auth/users")
    async def create_console_user(request: Request) -> JSONResponse:
        user = users_manager(request)
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
            password = seed_user(ctx.db, email, data["role"], display_name,
                                 public_key=settings.license_public_key)
        except ValueError as exc:
            from . import licensing
            if isinstance(exc, licensing.LicenseError):
                status = 423 if "read-only" in str(exc).lower() else 409
            else:
                status = 422
            return JSONResponse({"detail": str(exc)}, status_code=status)
        if password is None:
            return JSONResponse({"detail": "A console user with this email already exists."}, status_code=409)
        audit("create_user", email, True, "created")
        audit_log.record(ctx.db, user.subject, "people", "person_added", email, {"roles": [data["role"]]},
                         client_ip(request, settings.trusted_proxies))
        return JSONResponse({"email": email, "name": display_name.strip(), "role": data["role"],
                             "initial_password": password,
                             "message": "Account created. Copy the initial password and share it securely."},
                            status_code=201, headers={"Cache-Control": "no-store"})

    from .route_registry import mount_discovered_routers
    mount_discovered_routers(app)

    if not settings.console_login:
        install_security(app, ctx)
        return

    def api_permission(path: str) -> str:
        """Return the view permission that owns a console API path."""
        if path in ("/v1/system-check", "/v1/support-bundle"):
            return "node.operations.read"
        if path in ("/v1/role-health", "/v1/master/autonomy", "/v1/master/report") \
                or path == "/v1/provider-routing" or path.startswith("/v1/provider-routing/decisions"):
            return "view.machines_ai"
        if path in ("/v1/role-health/settings", "/v1/master/autonomy/budget", "/v1/provider-routing/settings"):
            return "console.manage"
        if path.startswith(("/v1/console/users", "/v1/console/audit", "/v1/console/view-as")):
            return "console.manage"
        if path.startswith(("/v1/runtime-settings", "/v1/quota-refresh/")):
            return "console.manage"
        if path.startswith("/v1/runs/") and path.endswith("/inbox"):
            return "review.actions"
        if path.startswith("/v1/knowledge/"):
            return "view.knowledge"
        if path.startswith("/v1/integrations/"):
            return "view.integrations"
        if path == "/v1/reviews" or path.startswith("/v1/reviews/"):
            return "view.human_review"
        if path.startswith("/v1/business/"):
            return "view.business"
        if path.startswith("/v1/stakeholder/"):
            return "view.stakeholder"
        if path.startswith(("/v1/nodes/", "/v1/accounts/", "/v1/services", "/v1/sa/")):
            return "view.machines_ai"
        if path == "/v1/logs" or path.startswith(("/v1/log-sources", "/v1/logs/")):
            return "view.logs"
        if path.startswith("/v1/projects"):
            if "/study-cases" in path:
                return "view.knowledge"
            if any(suffix in path for suffix in ("/versions", "/report", "/close")):
                return "view.delivery"
            return "projects.manage"
        if path.startswith("/v1/memory/"):
            return "view.knowledge"
        if path.startswith("/v1/tickets/"):
            return "view.work_board"
        if path.startswith("/v1/analytics/") or path == "/v1/state":
            return "view.dashboard"
        return "view.dashboard"

    def project_for_path(request: Request) -> str:
        path = request.url.path
        match = re.match(r"^/v1/(?:business|stakeholder|projects)/([^/]+)", path)
        if match:
            return match.group(1)
        match = re.match(r"^/v1/tickets/([^/]+)", path)
        if match:
            row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (match.group(1),))
            return str((row or {}).get("project") or "")
        match = re.match(r"^/v1/reviews/([^/]+)", path)
        if match:
            row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (match.group(1),))
            return str((row or {}).get("project") or "")
        if path.startswith(("/v1/analytics/", "/v1/memory/")) or path in ("/v1/reviews", "/v1/reviews/export.csv"):
            return request.query_params.get("project", "")
        return ""

    def audit_refusal(user: ConsoleUser, request: Request, reason: str = "not available for your role") -> None:
        ctx.db.run("INSERT INTO write_audit (target,action,ticket,run_id,fence,actor,accepted,result,payload)"
                   " VALUES ('console','access_denied','', '', NULL,%s,false,%s,%s)",
                   (user.subject, reason, J({"path": request.url.path, "method": request.method,
                                             "roles": list(user.effective_roles)})))

    def active_user(request: Request, allow_pending: bool = False) -> ConsoleUser | None:
        # verify_session also refuses an ended session (forced sign-out) and a pre-hardening cookie
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret, None,
                                allow_pending)
        return resolve_session_user(ctx.db, claims, allow_pending) if claims else None

    def forbidden_page() -> HTMLResponse:
        return HTMLResponse("<!doctype html><html lang='en'><meta charset='utf-8'><title>View unavailable</title>"
                            "<main style='font:16px system-ui;max-width:42rem;margin:12vh auto;padding:2rem'>"
                            "<h1>View unavailable</h1><p>This view is not available for your role.</p>"
                            "<a href='/cc'>Return to Dashboard</a></main></html>", status_code=403,
                            headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        setup_path = path == "/setup" or path.startswith("/v1/setup/")
        setup_open = setup_path and setup_wizard.user_count(ctx) == 0
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES) or setup_open:
            return await call_next(request)
        bearer_route = bool(request.headers.get("authorization") and checks_token(request))
        user = await run_in_threadpool(active_user, request)  # reads the database (ended sessions): off the loop
        if user is None and bearer_route:
            return await call_next(request)
        if user is None and await run_in_threadpool(active_user, request, True):
            # signed in but two-factor setup is required first: only the page shell and the setup routes
            if path in PAGES or path.startswith("/v1/security/mfa") or path == "/v1/auth/me":
                return await call_next(request)
            return JSONResponse({"detail": "set up two-factor sign-in first", "mfa_setup_required": True}, status_code=403)
        if user is None:
            if path in PAGES or path.startswith("/help/"):
                target = safe_next(path + (f"?{request.url.query}" if request.url.query else ""))
                return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=302)
            if request.cookies.get(COOKIE_NAME):
                return JSONResponse({"detail": "not signed in"}, status_code=401)
            return JSONResponse({"detail": "sign in required"}, status_code=401)

        if path == "/" and not user.view_as:
            return RedirectResponse(landing_page(user.assigned_roles), status_code=302)
        page_view = PAGE_VIEWS.get(path)
        required = permissions_for_view(page_view) if page_view else api_permission(path)
        view_as_stop = path == "/v1/console/view-as/stop"
        if view_as_stop:
            allowed = user.is_administrator
        else:
            allowed = has_permission(user.effective_roles, required)
        project = project_for_path(request)
        if allowed and project and user.project_keys is not None:
            allowed = project in user.project_keys
        if not allowed:
            audit_refusal(user, request)
            if page_view:
                return forbidden_page()
            return JSONResponse({"detail": "not available for your role"}, status_code=403)
        if page_view is not None:
            request.state.console_user = user
        if path in PAGES and user.view_as:
            request.state.console_user = user
        if path.startswith("/v1/"):
            request.state.console_user = user
        if bearer_route and request.cookies.get(COOKIE_NAME) and user:
            # A signed-in person cannot use a separate bearer header to skip the role menu.
            return await call_next(request)
        if path in PAGES or path.startswith("/help/"):
            target = safe_next(path + (f"?{request.url.query}" if request.url.query else ""))
            # Retain the current page for an already authenticated browser; sign-in itself lands by role priority.
            request.state.console_user = user
        return await call_next(request)

    install_security(app, ctx)
