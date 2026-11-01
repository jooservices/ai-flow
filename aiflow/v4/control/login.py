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
import os
import secrets
import stat
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .application_context import ApplicationContext
from . import audit as audit_log
from . import console_auth, mfa  # noqa: F401 - retain the historical login module exports
from .console_auth import (COOKIE_NAME, PAGE_VIEWS, ROLE_LABELS, ROLE_PERMISSIONS, ConsoleUser, csrf_token,  # noqa: F401
                           has_permission, issue_session, landing_page, normalize_roles, permissions_for_view,
                           resolve_session_user, verify_session)
from .db import J
from .emails import EMAIL, normalize_email  # noqa: F401 - re-exported for callers of login.normalize_email
from .ui import CONSOLE_PAGE_PATHS
from .login_sessions import (CSRF_COOKIE as CSRF_COOKIE, SESSION_TTL_MAX_S, SESSION_TTL_MIN_S, SESSION_TTL_S)

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


def register_login_endpoint(app: FastAPI, ctx: ApplicationContext) -> None:
    from .login_signin import SignInParts

    parts = SignInParts(ctx, ctx.settings)

    @app.post("/v1/auth/login")
    async def login(request: Request) -> JSONResponse:
        from .login_signin import login_response

        return await login_response(parts.ctx, request)


def install_login(app: FastAPI, ctx: ApplicationContext) -> None:
    from .login_gate import install_gate
    from .login_sessions import install_sessions, register_session_revocation
    from .login_signin import install_signin
    from .login_users import install_users
    from .route_registry import mount_discovered_routers
    from .security import install_security

    register_session_revocation(ctx)
    install_signin(app, ctx)
    register_login_endpoint(app, ctx)
    install_sessions(app, ctx)
    install_users(app, ctx)
    mount_discovered_routers(app)
    if ctx.settings.console_login:
        install_gate(app, ctx)
    install_security(app, ctx)
