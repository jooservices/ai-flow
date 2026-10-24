"""Console login: people sign in with an email and a password and get the signed `aiflow4_session` cookie.

`console_auth.py` defines the cookie and the roles; this module adds the users (table `console_users`), the
password hashing (scrypt from the standard library, salted, no new dependency), the sign-in page, the endpoints
and the gate. The gate only works with `AIFLOW4_CONSOLE_LOGIN=1`; without it nothing changes.

Sign-in, in order:
1. Redis rate limits per client IP and per IP+email, before any database lookup (429 with Retry-After).
2. The user row; an account whose lock has expired gets `failed_attempts` reset first.
3. scrypt verification in a worker thread, never on the event loop. Unknown emails and locked accounts are checked
   against a dummy hash, so every refusal costs the same and returns the same 401 answer.
4. A wrong password counts a failure; MAX_FAILURES in a row lock the account for LOCK_S.
Failed sign-ins are audited, but at most AUDIT_FAILED_PER_MIN rows a minute, so a flood cannot grow the table.

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
from urllib.parse import quote

import redis
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from .console_auth import (COOKIE_NAME, PAGE_VIEWS, ROLE_LABELS, ROLE_PERMISSIONS, ConsoleUser, has_permission,
                           issue_session, landing_page, normalize_roles, permissions_for_view, resolve_session_user,
                           verify_session)
from .db import J
from .ui import CONSOLE_PAGE_PATHS

log = logging.getLogger(__name__)

SESSION_TTL_S = 12 * 3600
MAX_FAILURES = 5          # wrong passwords in a row before the account is locked
LOCK_S = 15 * 60
RATE_WINDOW_S = 15 * 60
RATE_PER_IP = 30          # sign-in attempts per client IP per window
RATE_PER_IP_EMAIL = 10    # sign-in attempts per client IP and email per window
AUDIT_FAILED_PER_MIN = 30
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}")
REFUSED = "invalid email or password"

PAGES = frozenset(CONSOLE_PAGE_PATHS) | frozenset(PAGE_VIEWS)
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
def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL.fullmatch(email):
        raise ValueError("enter a valid email address")
    return email


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
    with db.pool.connection() as c:
        updated = c.execute("UPDATE console_users SET password_hash=%s, failed_attempts=0, locked_until=NULL,"
                            " updated_at=now() WHERE email=%s RETURNING email", (hash_password(password), email)).fetchone()
        if not updated:
            raise ValueError("no such console user")
        if deliver:
            deliver(password)
    return password


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
body:JSON.stringify({email:document.getElementById("email").value,password:document.getElementById("password").value})})
.then(function(r){return r.json().then(function(d){return[r.status,d]})})
.then(function(x){if(x[0]===200){location.href=x[1].landing_page||next}
else{m.textContent=x[1].detail||"Sign-in failed";b.disabled=false}})
.catch(function(){m.textContent="Cannot reach the server";b.disabled=false})});
</script></body></html>
"""


def install_login(app: FastAPI, ctx) -> None:
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

    def checks_token(request: Request) -> bool:
        for route in app.routes:
            if route.matches(request.scope)[0] == Match.FULL:
                if id(route) not in token_routes:
                    token_routes[id(route)] = isinstance(route, APIRoute) and route_checks_token(route)
                return token_routes[id(route)]
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

    def attempt(ip: str, email: str, password: str) -> tuple[int, dict, dict]:
        """The blocking part of a sign-in (Redis, database, scrypt); runs in a worker thread."""
        wait = over_limit(ip, email)
        if wait:
            return 429, {"detail": "too many sign-in attempts; try again later"}, {"Retry-After": str(wait)}
        row = None
        if email:
            ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, updated_at=now()"
                       " WHERE email=%s AND locked_until IS NOT NULL AND locked_until <= now()", (email,))
            row = ctx.db.one("SELECT email, display_name, role, roles, password_hash, disabled, locked_until"
                             " FROM console_users WHERE email=%s", (email,))
        locked = bool(row and row["locked_until"])
        usable = bool(row and not locked and not row["disabled"])
        good = verify_password(password, row["password_hash"] if usable else _dummy_hash())
        if not (usable and good):
            if usable:
                ctx.db.run("UPDATE console_users SET failed_attempts=failed_attempts+1,"
                           " locked_until=CASE WHEN failed_attempts+1 >= %s THEN now() + make_interval(secs => %s)"
                           " ELSE locked_until END, updated_at=now() WHERE email=%s", (MAX_FAILURES, LOCK_S, email))
            audit_failure(email, "account locked" if locked else REFUSED)
            return 401, {"detail": REFUSED}, {}
        ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, last_login_at=now(), updated_at=now()"
                   " WHERE email=%s", (email,))
        ctx.leases.r.delete(key("ip_email", digest(f"{ip}\n{email}")))
        audit("login", email, True, "signed in")
        return 200, user_json(email, row.get("roles") or [row["role"]], row["display_name"]), {}

    @app.get("/login", include_in_schema=False)
    def login_page() -> HTMLResponse:
        return HTMLResponse(LOGIN_PAGE, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post("/v1/auth/login")
    async def login(request: Request) -> JSONResponse:
        secret = settings.console_session_secret
        if len(secret.encode()) < 32:
            return JSONResponse({"detail": "console sign-in is not configured"}, status_code=503)
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
            status, body, headers = await run_in_threadpool(
                attempt, client_ip(request, settings.trusted_proxies), email, data["password"][:1024])
        except redis.RedisError:
            log.exception("console sign-in: Redis is unavailable")
            return JSONResponse({"detail": "sign-in is unavailable; try again shortly"}, status_code=503)
        response = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **headers})
        if status == 200:
            response.set_cookie(COOKIE_NAME, issue_session(email, body["roles"], int(time.time()) + SESSION_TTL_S, secret),
                                max_age=SESSION_TTL_S, httponly=True, secure=secure_cookie(), samesite="strict", path="/")
        return response

    @app.post("/v1/auth/logout")
    def logout() -> JSONResponse:
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=secure_cookie(), samesite="strict")
        return response

    @app.get("/logout", include_in_schema=False)
    def logout_page() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=secure_cookie(), samesite="strict")
        return response

    @app.get("/v1/auth/me")
    def me(request: Request) -> JSONResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user:
            return JSONResponse({"detail": "not signed in"}, status_code=401)
        row = ctx.db.one("SELECT display_name FROM console_users WHERE email=%s", (user.subject,))
        data = user_json(user.subject, list(user.assigned_roles), (row or {}).get("display_name", ""))
        data["landing_page"] = landing_page(user.effective_roles)
        return JSONResponse(data)

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
        return JSONResponse({"email": email, "name": display_name.strip(), "role": data["role"],
                             "initial_password": password,
                             "message": "Account created. Copy the initial password and share it securely."},
                            status_code=201, headers={"Cache-Control": "no-store"})

    if not settings.console_login:
        return

    def api_permission(path: str) -> str:
        """Return the view permission that owns a console API path."""
        if path in ("/v1/role-health", "/v1/master/autonomy", "/v1/master/report"):
            return "view.machines_ai"
        if path in ("/v1/role-health/settings", "/v1/master/autonomy/budget"):
            return "console.manage"
        if path.startswith(("/v1/console/users", "/v1/console/audit", "/v1/console/view-as")):
            return "console.manage"
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

    def active_user(request: Request) -> ConsoleUser | None:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret)
        return resolve_session_user(ctx.db, claims) if claims else None

    def forbidden_page() -> HTMLResponse:
        return HTMLResponse("<!doctype html><html lang='en'><meta charset='utf-8'><title>View unavailable</title>"
                            "<main style='font:16px system-ui;max-width:42rem;margin:12vh auto;padding:2rem'>"
                            "<h1>View unavailable</h1><p>This view is not available for your role.</p>"
                            "<a href='/cc'>Return to Dashboard</a></main></html>", status_code=403,
                            headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        bearer_route = bool(request.headers.get("authorization") and checks_token(request))
        user = active_user(request)
        if user is None and bearer_route:
            return await call_next(request)
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
