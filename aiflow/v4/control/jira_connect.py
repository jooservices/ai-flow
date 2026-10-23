"""Connect Jira from the Integrations screen: OAuth 2.0 (3LO) with Atlassian, or an API token as fallback.

Secret store: JSON files under `jira_secret_dir` (/etc/aiflow/secrets/jira, directory 0700, files 0600, written
atomically), shared by control-api and control-loops. `oauth-app.json` holds the client id and secret of the
owner's Atlassian OAuth app, `credential.json` the connected credential (OAuth tokens or email + API token) and
non-secret site details, `disconnected.json` who disconnected and when. Tokens never go to the registry, to logs,
to events or to any API response; audit rows carry only the kind, site and scopes.

Credential order (JiraAuth.credential): a credential connected on the UI, else nothing after an explicit
Disconnect, else JIRA_EMAIL / JIRA_API_TOKEN from owner.env. An OAuth access token is refreshed REFRESH_MARGIN_S
before it expires, or when Jira answers 401 (control/jira.py), under a file lock so two processes do not spend
the same rotating refresh token. A rejected refresh marks the credential `needs_reconnect`, writes an audit row
and opens the `jira_auth` incident; jira_sync then stops with that incident instead of failing silently.

Only a session with the `integrations.manage` permission (Owner and Super Administrator by default), or the owner
bearer token, may connect, store the fallback token or disconnect. The OAuth `state` is single-use, kept in Redis
for STATE_TTL_S and bound to the starting browser by a cookie; a callback with a missing or different state is
refused.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..core import secretbox
from .console_auth import COOKIE_NAME, checks_bearer, resolve_session_user, role_satisfies, verify_session
from .db import J
from . import licensing
from .incidents import close_incident, open_incident
from .jira import JiraError

log = logging.getLogger("aiflow4.jira_connect")

# Classic Jira scopes: read and write issues, comments, transitions and worklogs; offline_access for a refresh
# token. No manage:jira-configuration / manage:jira-project (admin) scope is requested.
SCOPES = ("read:jira-work", "write:jira-work", "offline_access")
REFRESH_MARGIN_S = 300
STATE_TTL_S = 600
STATE_COOKIE = "aiflow4_jira_oauth"
CALLBACK_PATH = "/v1/integrations/jira/oauth/callback"
INCIDENT_KIND, INCIDENT_SUBJECT = "jira_auth", "jira"
INTEGRATIONS_PAGE = "/integrations"


class JiraNotConnected(JiraError):
    """No usable Jira credential: never connected, disconnected, or the refresh token was rejected."""


class OAuthFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Credential:
    kind: str  # oauth | basic
    base_url: str
    secret: str = field(repr=False)
    email: str = field(default="", repr=False)
    source: str = ""

    def headers(self) -> dict[str, str]:
        if self.kind == "oauth":
            return {"Authorization": f"Bearer {self.secret}"}
        raw = base64.b64encode(f"{self.email}:{self.secret}".encode()).decode("ascii")
        return {"Authorization": f"Basic {raw}"}


def _iso(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return ""


def _now() -> float:
    return time.time()


class SecretStore:
    """Small JSON files with restricted permissions; the only place Jira secrets are written."""

    APP, CREDENTIAL, DISCONNECTED = "oauth-app.json", "credential.json", "disconnected.json"

    def __init__(self, path: str | Path) -> None:
        self.dir = Path(path)

    def _read(self, name: str) -> dict | None:
        try:
            raw = (self.dir / name).read_text(encoding="utf-8")
            data = json.loads(secretbox.decrypt(raw, context=name))
            if not secretbox.is_encrypted(raw):
                self._write(name, data)  # upgrade old plaintext files on first access
        except (OSError, ValueError):  # SecretBoxError is a ValueError: an unreadable secret reads as not connected
            return None
        return data if isinstance(data, dict) else None

    def _write(self, name: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            self.dir.chmod(0o700)  # a bind mount created by Docker starts out 0755
        tmp = self.dir / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secretbox.seal(json.dumps(data), name))  # fails closed if the external key is missing
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.dir / name)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def _delete(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.dir / name).unlink()

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def app(self) -> dict:
        return self._read(self.APP) or {}

    def save_app(self, client_id: str, client_secret: str) -> None:
        self._write(self.APP, {"client_id": client_id, "client_secret": client_secret})

    def credential(self) -> dict | None:
        return self._read(self.CREDENTIAL)

    def save_credential(self, data: dict) -> None:
        self._write(self.CREDENTIAL, data)

    def delete_credential(self) -> None:
        self._delete(self.CREDENTIAL)

    def disconnected(self) -> dict | None:
        return self._read(self.DISCONNECTED)

    def mark_disconnected(self, actor: str) -> None:
        self._write(self.DISCONNECTED, {"at": _now(), "by": actor})

    def clear_disconnected(self) -> None:
        self._delete(self.DISCONNECTED)


def _jwt_subject(token: str) -> str:
    """Atlassian access tokens are JWTs whose `sub` is the account id (used only if /myself is not readable)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(claims.get("sub") or "") if isinstance(claims, dict) else ""
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return ""


def _oauth_error(r: httpx.Response) -> str:
    """The error code of an OAuth answer, never its body (it could echo what we sent)."""
    try:
        data = r.json()
        code = str(data.get("error") or "") if isinstance(data, dict) else ""
    except ValueError:
        code = ""
    return f"HTTP {r.status_code}" + (f" ({code[:60]})" if code else "")


class JiraAuth:
    """Resolves the Jira credential in priority order and keeps an OAuth token fresh."""

    def __init__(self, settings, store: SecretStore, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.store = store
        self.http = http or httpx.Client(timeout=30.0, headers={"Accept": "application/json"})
        self.ctx = None
        self._lock = threading.Lock()

    def bind(self, ctx) -> "JiraAuth":
        """Audit rows and incidents need the registry; the CLI binds the context once it exists."""
        self.ctx = ctx
        return self

    @property
    def redirect_uri(self) -> str:
        return self.settings.public_url.rstrip("/") + CALLBACK_PATH

    def _legacy(self) -> bool:
        return bool(self.settings.jira_email and self.settings.jira_token)

    # --- state ----------------------------------------------------------------------------------------------
    def state(self) -> dict:
        data = self.store.credential()
        if data:
            source = "oauth" if data.get("kind") == "oauth" else "api_token"
            if data.get("status") == "needs_reconnect":
                return {"source": source, "usable": False, "reason": "needs_reconnect"}
            return {"source": source, "usable": True, "reason": "connected"}
        if self.store.disconnected():
            return {"source": "none", "usable": False, "reason": "disconnected"}
        if self._legacy():
            return {"source": "owner_env", "usable": True, "reason": "connected"}
        return {"source": "none", "usable": False, "reason": "not_connected"}

    def available(self) -> bool:
        return self.state()["usable"]

    def status(self) -> dict:
        """What the Integrations screen shows; an explicit whitelist, so no secret can slip into it."""
        st = self.state()
        data = self.store.credential() or {}
        app = self.store.app()
        gone = self.store.disconnected() or {}
        legacy = st["source"] == "owner_env"
        return {
            "connected": st["usable"], "source": st["source"], "state": st["reason"],
            "needs_reconnect": st["reason"] == "needs_reconnect",
            "kind": data.get("kind", "basic" if legacy else ""),
            "site_name": data.get("site_name", urlsplit(self.settings.jira_site).hostname if legacy else ""),
            "site_url": data.get("site_url", self.settings.jira_site if legacy else ""),
            "cloud_id": data.get("cloud_id", ""),
            "account": data.get("account", self.settings.jira_email if legacy else ""),
            "scopes": list(data.get("scopes") or []),
            "expires_at": _iso(data["expires_at"]) if data.get("expires_at") else "",
            "connected_at": _iso(data["connected_at"]) if data.get("connected_at") else "",
            "connected_by": data.get("connected_by", ""),
            "error": data.get("error", ""),
            "disconnected_at": _iso(gone["at"]) if gone.get("at") else "",
            "disconnected_by": gone.get("by", ""),
            "legacy_owner_env": self._legacy(),
            "oauth_app": {"configured": bool(app.get("client_id") and app.get("client_secret")),
                          "client_id": app.get("client_id", "")},
            "redirect_uri": self.redirect_uri,
            "requested_scopes": list(SCOPES),
        }

    # --- credential for the gateway -------------------------------------------------------------------------
    def credential(self, force_refresh: bool = False, stale: Credential | None = None) -> Credential:
        data = self.store.credential()
        if data:
            if data.get("status") == "needs_reconnect":
                raise JiraNotConnected("Jira needs to be reconnected on the Integrations screen")
            if data.get("kind") == "basic":
                return Credential("basic", str(data["site_url"]).rstrip("/"), str(data["api_token"]),
                                  str(data["email"]), "api_token")
            if force_refresh or float(data.get("expires_at") or 0) - _now() < REFRESH_MARGIN_S:
                data = self._refresh(force_refresh, stale)
            return self._oauth_credential(data)
        if self.store.disconnected():
            raise JiraNotConnected("Jira was disconnected on the Integrations screen")
        if self._legacy():
            return Credential("basic", self.settings.jira_site.rstrip("/"), self.settings.jira_token,
                              self.settings.jira_email, "owner_env")
        raise JiraNotConnected("Jira is not connected")

    def _oauth_credential(self, data: dict) -> Credential:
        base = f"{self.settings.jira_api_base.rstrip('/')}/{data['cloud_id']}"
        return Credential("oauth", base, str(data["access_token"]), source="oauth")

    def _refresh(self, forced: bool, stale: Credential | None) -> dict:
        with self._lock, self.store.lock():
            data = self.store.credential()
            if not data or data.get("kind") != "oauth":
                raise JiraNotConnected("Jira is not connected")
            if data.get("status") == "needs_reconnect":
                raise JiraNotConnected("Jira needs to be reconnected on the Integrations screen")
            expires_at = float(data.get("expires_at") or 0)
            # another process (or thread) refreshed while we waited for the lock
            if forced and stale is not None and data.get("access_token") != stale.secret:
                return data
            if not forced and expires_at - _now() >= REFRESH_MARGIN_S:
                return data
            app = self.store.app()
            if not (app.get("client_id") and app.get("client_secret") and data.get("refresh_token")):
                return self._refresh_rejected(data, "the OAuth app or refresh token is missing")
            try:
                r = self.http.post(self.settings.jira_oauth_token_url, json={
                    "grant_type": "refresh_token", "client_id": app["client_id"],
                    "client_secret": app["client_secret"], "refresh_token": data["refresh_token"]})
            except httpx.HTTPError as e:
                return self._refresh_transient(data, forced, type(e).__name__)
            if r.status_code >= 500 or r.status_code == 429:
                return self._refresh_transient(data, forced, _oauth_error(r))
            if r.status_code != 200:
                return self._refresh_rejected(data, _oauth_error(r))
            try:
                tokens = r.json()
                access = str(tokens["access_token"])
            except (ValueError, KeyError, TypeError):
                return self._refresh_transient(data, forced, "malformed token answer")
            data = {**data, "access_token": access,
                    "refresh_token": str(tokens.get("refresh_token") or data["refresh_token"]),
                    "expires_at": int(_now() + int(tokens.get("expires_in") or 3600)),
                    "refreshed_at": int(_now())}
            if tokens.get("scope"):
                data["scopes"] = str(tokens["scope"]).split()
            self.store.save_credential(data)
        log.info("jira oauth token refreshed for site %s", data.get("site_name", ""))
        self._audit("refresh", True, "access token refreshed", data)
        return data

    def _refresh_transient(self, data: dict, forced: bool, reason: str) -> dict:
        """Atlassian unreachable: keep a still valid token, else fail this call; the next pass tries again."""
        log.warning("jira oauth refresh failed (temporary): %s", reason)
        self._audit("refresh", False, f"temporary failure: {reason}", data)
        if not forced and float(data.get("expires_at") or 0) > _now():
            return data
        raise JiraError(f"Jira token refresh failed temporarily: {reason}")

    def _refresh_rejected(self, data: dict, reason: str) -> dict:
        """Refresh token revoked or expired: drop the tokens, keep the site details, ask for a reconnect."""
        kept = {k: v for k, v in data.items() if k not in ("access_token", "refresh_token")}
        kept.update(status="needs_reconnect", error=f"token refresh was rejected: {reason}"[:200],
                    failed_at=int(_now()))
        self.store.save_credential(kept)
        log.warning("jira oauth refresh rejected (%s); reconnect Jira on the Integrations screen", reason)
        self._audit("refresh", False, f"refresh rejected: {reason}", data)
        if self.ctx is not None:
            open_incident(self.ctx, INCIDENT_KIND, INCIDENT_SUBJECT, "critical",
                          "Jira needs to be reconnected",
                          f"Refreshing the Jira OAuth token for {data.get('site_name') or 'the site'} was rejected"
                          f" ({reason}). jira_sync and Jira writes are stopped. Open Integrations and press"
                          " Connect Jira again.")
        raise JiraNotConnected("Jira token refresh was rejected; reconnect Jira on the Integrations screen")

    def _audit(self, action: str, accepted: bool, result: str, data: dict | None = None,
               actor: str = "control") -> None:
        if self.ctx is None:
            return
        write_audit(self.ctx, action, actor, accepted, result, safe_payload(data or {}))

    # --- connecting -----------------------------------------------------------------------------------------
    def authorize_url(self, state: str) -> str:
        app = self.store.app()
        query = {"audience": "api.atlassian.com", "client_id": app["client_id"], "scope": " ".join(SCOPES),
                 "redirect_uri": self.redirect_uri, "state": state, "response_type": "code", "prompt": "consent"}
        return f"{self.settings.jira_oauth_authorize_url}?{urlencode(query)}"

    def connect_oauth(self, code: str, actor: str) -> dict:
        app = self.store.app()
        if not (app.get("client_id") and app.get("client_secret")):
            raise OAuthFailed("the OAuth app is not configured")
        try:
            r = self.http.post(self.settings.jira_oauth_token_url, json={
                "grant_type": "authorization_code", "client_id": app["client_id"],
                "client_secret": app["client_secret"], "code": code, "redirect_uri": self.redirect_uri})
        except httpx.HTTPError as e:
            raise OAuthFailed(f"token endpoint unreachable ({type(e).__name__})") from None
        if r.status_code != 200:
            raise OAuthFailed(f"code exchange refused: {_oauth_error(r)}")
        try:
            tokens = r.json()
            if not isinstance(tokens, dict):
                raise TypeError
            access, refresh = str(tokens.get("access_token") or ""), str(tokens.get("refresh_token") or "")
        except (ValueError, TypeError):
            raise OAuthFailed("malformed token answer") from None
        if not access:
            raise OAuthFailed("token answer is missing access_token")
        if not refresh:
            raise OAuthFailed("token answer is missing refresh_token")
        bearer = {"Authorization": f"Bearer {access}"}
        try:
            resources = self.http.get(self.settings.jira_oauth_resources_url, headers=bearer)
            sites = resources.json() if resources.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            sites = []
        sites = [s for s in sites if isinstance(s, dict) and s.get("id")] if isinstance(sites, list) else []
        if not sites:
            raise OAuthFailed("the token grants access to no Jira site")
        wanted = self.settings.jira_site.rstrip("/").lower()
        site = next((s for s in sites if str(s.get("url", "")).rstrip("/").lower() == wanted), sites[0])
        cloud_id = str(site["id"])
        account = ""
        try:
            me = self.http.get(f"{self.settings.jira_api_base.rstrip('/')}/{cloud_id}/rest/api/3/myself",
                               headers=bearer)
            if me.status_code == 200:
                body = me.json()
                account = str(body.get("emailAddress") or body.get("displayName") or body.get("accountId") or "")
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        scopes = str(tokens.get("scope") or "").split() or [str(s) for s in site.get("scopes") or []]
        data = {"kind": "oauth", "access_token": access, "refresh_token": refresh,
                "expires_at": int(_now() + int(tokens.get("expires_in") or 3600)),
                "scopes": scopes, "cloud_id": cloud_id, "site_name": str(site.get("name") or ""),
                "site_url": str(site.get("url") or ""), "account": account or _jwt_subject(access),
                "connected_at": int(_now()), "connected_by": actor}
        with self._lock, self.store.lock():
            self.store.save_credential(data)
            self.store.clear_disconnected()
        return data

    def connect_api_token(self, email: str, api_token: str, site: str, actor: str) -> dict:
        try:
            r = self.http.get(f"{site}/rest/api/3/myself",
                              headers=Credential("basic", site, api_token, email).headers())
        except httpx.HTTPError as e:
            raise OAuthFailed(f"the Jira site is unreachable ({type(e).__name__})") from None
        if r.status_code != 200:
            raise OAuthFailed(f"Atlassian rejected the email or API token (HTTP {r.status_code})")
        try:
            body = r.json()
            account = str(body.get("emailAddress") or body.get("displayName") or email)
        except (ValueError, AttributeError):
            account = email
        data = {"kind": "basic", "email": email, "api_token": api_token, "site_url": site,
                "site_name": urlsplit(site).hostname or site, "account": account, "scopes": [],
                "connected_at": int(_now()), "connected_by": actor}
        with self._lock, self.store.lock():
            self.store.save_credential(data)
            self.store.clear_disconnected()
        return data

    def disconnect(self, actor: str) -> dict:
        """Revoke at Atlassian when possible, then delete the credential and remember the disconnect."""
        revoked = False
        with self._lock, self.store.lock():
            data = self.store.credential() or {}
            app = self.store.app()
            token = data.get("refresh_token") or data.get("access_token")
            if data.get("kind") == "oauth" and token and app.get("client_id"):
                try:
                    r = self.http.post(self.settings.jira_oauth_revoke_url, json={
                        "token": token, "token_type_hint": "refresh_token" if data.get("refresh_token") else
                        "access_token", "client_id": app["client_id"], "client_secret": app.get("client_secret", "")})
                    revoked = r.status_code in (200, 204)
                    if not revoked:
                        log.warning("jira oauth revoke answered %s", _oauth_error(r))
                except httpx.HTTPError as e:
                    log.warning("jira oauth revoke failed: %s", type(e).__name__)
            self.store.delete_credential()
            self.store.mark_disconnected(actor)
        return {"data": data, "revoked": revoked}


# --- registry side effects ---------------------------------------------------------------------------------
def safe_payload(data: dict) -> dict:
    """Audit payload: which credential, never its value."""
    return {k: data[k] for k in ("kind", "site_name", "site_url", "cloud_id", "scopes") if data.get(k)}


def write_audit(ctx, action: str, actor: str, accepted: bool, result: str, payload: dict | None = None) -> None:
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
               " VALUES ('jira',%s,'',%s,%s,%s,%s)", (action, actor, accepted, result[:500], J(payload or {})))


def sync_gate(ctx) -> bool:
    """jira_sync runs only with a usable credential; otherwise the jira_auth incident says why (once)."""
    auth = getattr(ctx, "jira_auth", None)
    if auth is None:
        return bool(ctx.jira)
    st = auth.state()
    if st["usable"]:
        close_incident(ctx, INCIDENT_KIND, INCIDENT_SUBJECT, "Jira credential works again")
        return True
    title, detail = {
        "needs_reconnect": ("Jira needs to be reconnected",
                            "The Jira OAuth token could not be refreshed. Open Integrations and press Connect Jira."),
        "disconnected": ("Jira is disconnected",
                         "The owner disconnected Jira on the Integrations screen; jira_sync and Jira writes are"
                         " stopped until Jira is connected again."),
    }.get(st["reason"], ("Jira is not connected",
                         "No Jira credential: connect Jira on the Integrations screen (or set JIRA_EMAIL and"
                         " JIRA_API_TOKEN in owner.env). jira_sync is stopped."))
    if open_incident(ctx, INCIDENT_KIND, INCIDENT_SUBJECT, "critical" if st["reason"] == "needs_reconnect"
                     else "warning", title, detail):
        log.warning("jira_sync stopped: %s", title)
    return False


# --- HTTP endpoints ----------------------------------------------------------------------------------------
def _state_key(ctx, state: str) -> str:
    return f"{ctx.leases.p}jira_oauth_state:{hashlib.sha256(state.encode()).hexdigest()}"


def _clean(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _site(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query
            or parts.fragment or parts.path not in ("", "/")):
        return None
    return f"https://{parts.netloc}"


def mount(app: FastAPI, ctx) -> None:
    def auth() -> JiraAuth:
        a = getattr(ctx, "jira_auth", None)
        if a is None:
            raise HTTPException(503, "Jira connect is not configured")
        return a

    def owner(request: Request, action: str = "read") -> str:
        """The owner bearer token or a signed console session with `integrations.manage`; anyone else is refused."""
        def writable() -> None:
            try:
                licensing.require_writable(ctx.db, ctx.settings.license_public_key)
            except licensing.LicenseError as exc:
                raise HTTPException(423, str(exc)) from None

        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        configured = ctx.settings.owner_token
        if scheme.lower() == "bearer" and token.strip() and configured and secrets.compare_digest(
                token.strip().encode(), configured.encode()):
            if action != "read":
                writable()
            return "owner"
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if user and role_satisfies(user.effective_roles, {"integrations.manage"}):
            if action != "read":
                writable()
            return user.subject
        if action != "read":
            write_audit(ctx, action, user.subject if user else "", False, "not available for your role")
        if user:
            raise HTTPException(403, "not available for your role")
        raise HTTPException(401, "owner authorization required", headers={"WWW-Authenticate": "Bearer"})

    async def body_of(request: Request) -> dict:
        # parsed by hand: a validation error of a model would echo the submitted secret back in its answer
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise HTTPException(400, "expected a JSON object")
        return data

    @app.get("/v1/integrations/jira")
    @checks_bearer
    def jira_status(request: Request) -> dict:
        owner(request)
        return auth().status()

    @app.post("/v1/integrations/jira/oauth/start")
    @checks_bearer
    async def jira_oauth_start(request: Request):
        actor = owner(request, "connect")
        a = auth()
        data = await body_of(request)
        client_id, client_secret = _clean(data.get("client_id"), 200), _clean(data.get("client_secret"), 500)
        current = a.store.app()
        # an empty secret keeps the stored one (it is never shown again), as long as the client id is the same
        new_id = client_id or current.get("client_id", "")
        new_secret = client_secret or (current.get("client_secret", "") if current.get("client_id") == new_id else "")
        if new_id and new_secret and (new_id, new_secret) != (current.get("client_id"), current.get("client_secret")):
            a.store.save_app(new_id, new_secret)
            write_audit(ctx, "oauth_app", actor, True, "OAuth app saved", {"client_id": new_id})
        current = a.store.app()
        if client_id and current.get("client_id") != client_id:
            current = {}  # a new client id without its secret
        if not (current.get("client_id") and current.get("client_secret")):
            write_audit(ctx, "connect", actor, False, "OAuth app client id and secret are required")
            raise HTTPException(400, "enter the client id and client secret of the Atlassian OAuth app")
        state = secrets.token_urlsafe(32)
        ctx.leases.r.set(_state_key(ctx, state), json.dumps({"actor": actor, "at": int(_now())}), ex=STATE_TTL_S)
        response = JSONResponse({"authorize_url": a.authorize_url(state), "expires_in_s": STATE_TTL_S})
        response.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL_S, path="/v1/integrations/jira/oauth",
                            httponly=True, samesite="lax", secure=ctx.settings.public_url.startswith("https://"))
        return response

    @app.get(CALLBACK_PATH)
    def jira_oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        a = auth()
        cookie = request.cookies.get(STATE_COOKIE, "")
        pending = None
        if state:
            pipe = ctx.leases.r.pipeline()
            pipe.get(_state_key(ctx, state))
            pipe.delete(_state_key(ctx, state))
            raw, _ = pipe.execute()
            pending = json.loads(raw) if raw else None
        if not pending or not cookie or not secrets.compare_digest(state.encode(), cookie.encode()):
            write_audit(ctx, "connect", "", False, "OAuth callback refused: missing or invalid state")
            raise HTTPException(400, "invalid or expired OAuth state; press Connect Jira again")
        actor = str(pending.get("actor") or "owner")

        def back(result: str) -> RedirectResponse:
            response = RedirectResponse(f"{INTEGRATIONS_PAGE}?jira={result}", status_code=303)
            response.delete_cookie(STATE_COOKIE, path="/v1/integrations/jira/oauth")
            return response

        if error or not code:
            write_audit(ctx, "connect", actor, False, f"consent not given: {_clean(error, 60) or 'no code'}")
            return back("denied")
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        try:
            data = a.connect_oauth(code, actor)
        except OAuthFailed as e:
            log.warning("jira oauth connect failed: %s", e)
            write_audit(ctx, "connect", actor, False, f"OAuth connect failed: {e}")
            return back("failed")
        write_audit(ctx, "connect", actor, True, "connected with OAuth 2.0 (3LO)", safe_payload(data))
        ctx.events.emit("jira.connected", account=actor, outcome="oauth", reason=data.get("site_name", ""))
        close_incident(ctx, INCIDENT_KIND, INCIDENT_SUBJECT, "Jira connected")
        return back("connected")

    @app.post("/v1/integrations/jira/api-token")
    @checks_bearer
    async def jira_api_token(request: Request) -> dict:
        actor = owner(request, "connect")
        a = auth()
        data = await body_of(request)
        email, token = _clean(data.get("email"), 320), _clean(data.get("api_token"), 2000)
        site = _site(_clean(data.get("site"), 300) or ctx.settings.jira_site)
        if not email or "@" not in email or not token or not site:
            write_audit(ctx, "connect", actor, False, "fallback API token refused: email, API token and an"
                                                      " https site are required")
            raise HTTPException(400, "enter the Atlassian email, API token and an https:// site URL")
        try:
            saved = a.connect_api_token(email, token, site, actor)
        except OAuthFailed as e:
            write_audit(ctx, "connect", actor, False, f"fallback API token refused: {e}")
            raise HTTPException(400, str(e)) from None
        write_audit(ctx, "connect", actor, True, "connected with email and API token", safe_payload(saved))
        ctx.events.emit("jira.connected", account=actor, outcome="api_token", reason=saved["site_name"])
        close_incident(ctx, INCIDENT_KIND, INCIDENT_SUBJECT, "Jira connected")
        return a.status()

    @app.post("/v1/integrations/jira/disconnect")
    @checks_bearer
    def jira_disconnect(request: Request) -> dict:
        actor = owner(request, "disconnect")
        result = auth().disconnect(actor)
        write_audit(ctx, "disconnect", actor, True,
                    "disconnected; token revoked at Atlassian" if result["revoked"] else "disconnected",
                    {**safe_payload(result["data"]), "revoked": result["revoked"]})
        ctx.events.emit("jira.disconnected", account=actor, outcome="revoked" if result["revoked"] else "deleted")
        return {**auth().status(), "revoked": result["revoked"]}
