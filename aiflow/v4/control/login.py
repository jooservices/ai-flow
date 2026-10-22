"""Console login: people sign in with an email and a password and get the signed `aiflow4_session` cookie.

`console_auth.py` defines the cookie and the roles; this module adds the users (table `console_users`), the
password hashing (scrypt from the standard library, salted, no new dependency), the sign-in page, the endpoints
and the gate. The gate only works with `AIFLOW4_CONSOLE_LOGIN=1`; without it nothing changes.

Gate rules when it is on:
- a console page without a valid session redirects to `/login?next=...`; any other path answers 401;
- a request that carries an `Authorization` header passes only for routes that check a token themselves
  (nodes, workers, reviewer, owner, release files), because an attacker could add the header to any request;
- `/healthz`, `/login`, `/logout`, `/v1/auth/*` and the node join/install endpoints stay open (join grants carry
  their own one-use token).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from starlette.routing import Match

from .console_auth import COOKIE_NAME, ROLE_LABELS, ROLE_PERMISSIONS, issue_session, verify_session
from .db import J

SESSION_TTL_S = 12 * 3600
MAX_FAILURES = 5          # wrong passwords in a row before the account is locked
LOCK_S = 15 * 60
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}")

PAGES = {"/", "/cc", "/delivery", "/sa", "/lead", "/business", "/review", "/stakeholder", "/knowledge",
         "/integrations", "/projects"}
OPEN_PATHS = {"/login", "/logout", "/healthz", "/v1/install.sh", "/v1/nodes/join", "/v1/nodes/join-tokens",
              "/v1/nodes/join-tokens/inspect"}
OPEN_PREFIXES = ("/v1/auth/",)
TOKEN_CHECKS = {"node_auth", "owner_auth", "reviewer_auth", "release_reader"}  # dependencies that verify a bearer token


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


def seed_user(db, email: str, role: str, display_name: str = "") -> str | None:
    """Create the user with a generated password and return it; return None when the user already exists."""
    email = normalize_email(email)
    if role not in ROLE_PERMISSIONS:
        raise ValueError(f"unknown role {role}; choose one of {', '.join(sorted(ROLE_PERMISSIONS))}")
    if db.one("SELECT 1 AS x FROM console_users WHERE email=%s", (email,)):
        return None
    password = generate_password()
    db.run("INSERT INTO console_users (email, display_name, role, password_hash) VALUES (%s,%s,%s,%s)"
           " ON CONFLICT (email) DO NOTHING", (email, display_name.strip(), role, hash_password(password)))
    return password


def reset_password(db, email: str) -> str:
    """Give an existing user a new generated password and clear any lock."""
    email = normalize_email(email)
    if not db.one("SELECT 1 AS x FROM console_users WHERE email=%s", (email,)):
        raise ValueError("no such console user")
    password = generate_password()
    db.run("UPDATE console_users SET password_hash=%s, failed_attempts=0, locked_until=NULL, updated_at=now()"
           " WHERE email=%s", (hash_password(password), email))
    return password


# --- HTTP ----------------------------------------------------------------------------------------------------------
def safe_next(value: str) -> str:
    """Only a path on this site: no scheme, no host, no protocol-relative URL."""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value:
        return value
    return "/cc"


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · AI Flow</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#14171c;--muted:#5d6672;--line:#d9dde3;--accent:#2557d6;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--text:#e8ebef;--muted:#9aa4b0;--line:#2a313a;--accent:#7aa2ff;--err:#ff8b84}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:16px}
form{width:100%;max-width:360px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 20px;color:var(--muted);font-size:13px}
label{display:block;font-size:13px;margin:12px 0 4px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--text);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:20px;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:default}#msg{min-height:20px;margin-top:12px;color:var(--err);font-size:13px}
</style></head><body>
<form id="f" autocomplete="on"><h1>AI Flow Command Center</h1><p>Sign in to continue.</p>
<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required autofocus>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button id="go" type="submit">Sign in</button><div id="msg" role="alert"></div></form>
<script>
var next=new URLSearchParams(location.search).get("next")||"/cc";
document.getElementById("f").addEventListener("submit",function(e){e.preventDefault();
var b=document.getElementById("go"),m=document.getElementById("msg");b.disabled=true;m.textContent="";
fetch("/v1/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",
body:JSON.stringify({email:document.getElementById("email").value,password:document.getElementById("password").value})})
.then(function(r){return r.json().then(function(d){return[r.status,d]})})
.then(function(x){if(x[0]===200){location.href=next.charAt(0)==="/"&&next.charAt(1)!=="/"?next:"/cc"}
else{m.textContent=x[1].detail||"Sign-in failed";b.disabled=false}})
.catch(function(){m.textContent="Cannot reach the server";b.disabled=false})});
</script></body></html>
"""


def install_login(app: FastAPI, ctx) -> None:
    settings = ctx.settings
    token_route_ids: set[int] = set()

    def flatten(dependant):
        yield dependant.call
        for sub in dependant.dependencies:
            yield from flatten(sub)

    def checks_token(request: Request) -> bool:
        if not token_route_ids:
            for route in app.routes:
                if isinstance(route, APIRoute) and any(getattr(c, "__name__", "") in TOKEN_CHECKS
                                                       for c in flatten(route.dependant)):
                    token_route_ids.add(id(route))
        for route in app.routes:
            if route.matches(request.scope)[0] == Match.FULL:
                return id(route) in token_route_ids
        return False

    def audit(action: str, email: str, accepted: bool, result: str) -> None:
        ctx.db.run("INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", ("console", action, "", "", None, email, accepted, result, J({})))

    def secure_cookie() -> bool:
        return str(settings.public_url or "").lower().startswith("https://")

    def user_json(email: str, role: str, name: str = "") -> dict:
        return {"email": email, "name": name, "role": role, "role_label": ROLE_LABELS.get(role, role)}

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
        row = ctx.db.one("SELECT email, display_name, role, password_hash, disabled, failed_attempts, locked_until"
                         " FROM console_users WHERE email=%s", (email,)) if email else None
        if row and row["locked_until"] and row["locked_until"].timestamp() > time.time():
            audit("login", email, False, "account locked")
            return JSONResponse({"detail": "too many attempts; try again later"}, status_code=429,
                                headers={"Retry-After": str(LOCK_S)})
        good = verify_password(data["password"], row["password_hash"] if row else _dummy_hash())
        if not (row and good and not row["disabled"]):
            if row:
                ctx.db.run("UPDATE console_users SET failed_attempts=failed_attempts+1,"
                           " locked_until=CASE WHEN failed_attempts+1 >= %s THEN now() + make_interval(secs => %s)"
                           " ELSE locked_until END, updated_at=now() WHERE email=%s", (MAX_FAILURES, LOCK_S, email))
            audit("login", email, False, "invalid email or password")
            return JSONResponse({"detail": "invalid email or password"}, status_code=401)
        ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, last_login_at=now(), updated_at=now()"
                   " WHERE email=%s", (email,))
        audit("login", email, True, "signed in")
        response = JSONResponse(user_json(email, row["role"], row["display_name"]))
        response.set_cookie(COOKIE_NAME, issue_session(email, row["role"], int(time.time()) + SESSION_TTL_S, secret),
                            max_age=SESSION_TTL_S, httponly=True, secure=secure_cookie(), samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/v1/auth/logout")
    def logout() -> JSONResponse:
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/logout", include_in_schema=False)
    def logout_page() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/v1/auth/me")
    def me(request: Request) -> JSONResponse:
        user = verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret)
        if not user:
            return JSONResponse({"detail": "not signed in"}, status_code=401)
        row = ctx.db.one("SELECT display_name FROM console_users WHERE email=%s", (user.subject,))
        return JSONResponse(user_json(user.subject, user.role, (row or {}).get("display_name", "")))

    if not settings.console_login:
        return

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        if request.headers.get("authorization") and checks_token(request):
            return await call_next(request)
        if verify_session(request.cookies.get(COOKIE_NAME, ""), settings.console_session_secret):
            return await call_next(request)
        if path in PAGES:
            target = safe_next(path + (f"?{request.url.query}" if request.url.query else ""))
            return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=302)
        return JSONResponse({"detail": "sign in required"}, status_code=401)
