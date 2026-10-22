"""Connect Jira from the Integrations screen (TESTFLOW-79): OAuth 2.0 (3LO) against a fake Atlassian server
(authorize, token, refresh, revoke, accessible-resources and the Jira REST API behind a cloud id), the API-token
fallback, owner.env fallback, owner-only access, refresh, incidents, Disconnect and secret hygiene."""
from __future__ import annotations

import base64
import json
import logging
import stat
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.jira import Jira, JiraError
from aiflow.v4.control.jira_connect import CALLBACK_PATH, SCOPES, JiraAuth, SecretStore
from aiflow.v4.control.loops import jira_sync

SESSION_SECRET = "s" * 40
AUTH, API, SITE = "https://auth.fake-atlassian.test", "https://api.fake-atlassian.test", "https://acme.atlassian.net"
CLOUD_ID = "cloud-0b5e"
CLIENT_ID, CLIENT_SECRET = "client-id-123", f"CLIENT-SECRET-{uuid.uuid4().hex}"


class FakeAtlassian:
    """Just enough of auth.atlassian.com, api.atlassian.com and a Jira site, served through httpx.MockTransport."""

    def __init__(self) -> None:
        self.codes: dict[str, str] = {}  # code -> redirect_uri
        self.access: dict[str, float] = {}  # valid access token -> expiry
        self.refresh: set[str] = set()
        self.revoked: list[str] = []
        self.issued: list[str] = []  # every secret handed out, to search for leaks
        self.grants: list[str] = []
        self.requests: list[httpx.Request] = []
        self.omit_refresh_for_authorization = False
        self.basic = {("bot@acme.test", f"API-TOKEN-{uuid.uuid4().hex}")}
        self.tickets = [{"key": "TESTWP-1", "fields": {
            "summary": "Synced over the new credential", "status": {"name": "To Do"}, "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"}, "labels": ["aiflow-ready"], "description": None,
            "created": "2026-10-01T10:00:00.000+0000", "updated": "2026-10-20T10:00:00.000+0000"}}]
        self.status_of = {"TESTWP-1": "To Do"}
        self.comments: list[tuple[str, str]] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=False)

    def _tokens(self, include_refresh: bool = True) -> dict:
        access = f"ACCESS-{uuid.uuid4().hex}"
        self.access[access] = time.time() + 3600
        self.issued.append(access)
        tokens = {"access_token": access, "expires_in": 3600, "token_type": "Bearer", "scope": " ".join(SCOPES)}
        if include_refresh:
            refresh = f"REFRESH-{uuid.uuid4().hex}"
            self.refresh.add(refresh)
            self.issued.append(refresh)
            tokens["refresh_token"] = refresh
        return tokens

    def _bearer_ok(self, request: httpx.Request) -> bool:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        return self.access.get(token, 0) > time.time()

    def _basic_ok(self, request: httpx.Request) -> bool:
        raw = request.headers.get("authorization", "")
        if not raw.startswith("Basic "):
            return False
        email, _, token = base64.b64decode(raw[6:]).decode().partition(":")
        return (email, token) in self.basic

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(f"{AUTH}/authorize"):  # the owner presses Accept on the consent page
            q = parse_qs(request.url.query.decode())
            code = f"code-{uuid.uuid4().hex[:8]}"
            self.codes[code] = q["redirect_uri"][0]
            return httpx.Response(302, headers={"Location": f"{q['redirect_uri'][0]}?code={code}&state={q['state'][0]}"})
        if url == f"{AUTH}/oauth/token":
            body = json.loads(request.content)
            self.grants.append(body["grant_type"])
            if body.get("client_id") != CLIENT_ID or body.get("client_secret") != CLIENT_SECRET:
                return httpx.Response(401, json={"error": "access_denied"})
            if body["grant_type"] == "authorization_code":
                if self.codes.pop(body.get("code"), None) != body.get("redirect_uri"):
                    return httpx.Response(403, json={"error": "invalid_grant"})
                include_refresh = not self.omit_refresh_for_authorization
                self.omit_refresh_for_authorization = False
                return httpx.Response(200, json=self._tokens(include_refresh=include_refresh))
            if body.get("refresh_token") not in self.refresh:
                return httpx.Response(403, json={"error": "unauthorized_client",
                                                 "error_description": "refresh_token is invalid"})
            self.refresh.discard(body["refresh_token"])  # rotating refresh tokens
            return httpx.Response(200, json=self._tokens())
        if url == f"{AUTH}/oauth/revoke":
            token = json.loads(request.content)["token"]
            self.revoked.append(token)
            self.refresh.discard(token)
            self.access.pop(token, None)
            return httpx.Response(200)
        if url == f"{API}/oauth/token/accessible-resources":
            if not self._bearer_ok(request):
                return httpx.Response(401)
            return httpx.Response(200, json=[{"id": CLOUD_ID, "url": SITE, "name": "acme", "scopes": list(SCOPES)}])
        if url.startswith(f"{API}/ex/jira/{CLOUD_ID}/"):
            if not self._bearer_ok(request):
                return httpx.Response(401, json={"message": "Unauthorized"})
            return self.jira(request, request.url.path.removeprefix(f"/ex/jira/{CLOUD_ID}"))
        if url.startswith(f"{SITE}/"):
            if not self._basic_ok(request):
                return httpx.Response(401, json={"message": "Unauthorized"})
            return self.jira(request, request.url.path)
        return httpx.Response(404)

    def jira(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "557058:bot", "displayName": "AI Flow bot",
                                             "emailAddress": "bot@acme.test"})
        if path == "/rest/api/3/project/TESTWP/versions":
            return httpx.Response(200, json=[])
        if path == "/rest/api/3/search/jql":
            return httpx.Response(200, json={"issues": self.tickets, "isLast": True})
        if path.startswith("/rest/api/3/issue/"):
            key = path.split("/")[5]
            if path.endswith("/transitions") and request.method == "GET":
                return httpx.Response(200, json={"transitions": [{"id": "31", "to": {"name": "In Progress"}}]})
            if path.endswith("/transitions"):
                self.status_of[key] = "In Progress"
                return httpx.Response(204)
            if path.endswith("/comment"):
                self.comments.append((key, json.dumps(json.loads(request.content)["body"])))
                return httpx.Response(201, json={"id": "10001"})
            if request.method == "PUT":
                return httpx.Response(204)
            return httpx.Response(200, json={"fields": {"status": {"name": self.status_of.get(key, "To Do")}}})
        return httpx.Response(404)

    def expire_access(self) -> None:
        for token in self.access:
            self.access[token] = 0


@pytest.fixture
def atlassian():
    return FakeAtlassian()


@pytest.fixture
def jira_env(ctx, tmp_path, atlassian):
    s = ctx.settings
    s.jira_site, s.public_url, s.console_session_secret = SITE, "https://control.example.test", SESSION_SECRET
    s.jira_oauth_authorize_url, s.jira_oauth_token_url = f"{AUTH}/authorize", f"{AUTH}/oauth/token"
    s.jira_oauth_revoke_url = f"{AUTH}/oauth/revoke"
    s.jira_oauth_resources_url, s.jira_api_base = f"{API}/oauth/token/accessible-resources", f"{API}/ex/jira"
    s.jira_secret_dir = str(tmp_path / "secrets" / "jira")
    auth = JiraAuth(s, SecretStore(s.jira_secret_dir), http=atlassian.client).bind(ctx)
    ctx.jira_auth = auth
    ctx.jira = Jira(s.jira_site, auth=auth, client=atlassian.client)
    return ctx


@pytest.fixture
def web(jira_env):
    # https base URL: the state cookie is Secure because the public URL is https
    return TestClient(create_app(jira_env), base_url="https://control.example.test")


def sign_in(web, role="owner", subject="owner@example.test"):
    web.cookies.set("aiflow4_session", issue_session(subject, role, 2_000_000_000, SESSION_SECRET))


def connect_oauth(web, atlassian) -> httpx.Response:
    start = web.post("/v1/integrations/jira/oauth/start", json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert start.status_code == 200, start.text
    consent = atlassian.client.get(start.json()["authorize_url"])
    assert consent.status_code == 302
    back = urlsplit(consent.headers["Location"])
    assert back.path == CALLBACK_PATH
    return web.get(f"{back.path}?{back.query}", follow_redirects=False)


def audit_rows(ctx, action=None):
    rows = ctx.db.all("SELECT action, actor, accepted, result, payload FROM write_audit WHERE target='jira' ORDER BY id")
    return [r for r in rows if action is None or r["action"] == action]


def registry_text(ctx) -> str:
    tables = [r["table_name"] for r in ctx.db.all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")]
    return "\n".join(str(r["row"]) for t in tables for r in ctx.db.all(f'SELECT t::text AS row FROM "{t}" t'))


def test_deployment_uses_control_environment_file_for_callback_public_url():
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "deploy/v4/control/compose.yaml").read_text(encoding="utf-8"))
    app = compose["x-app"]

    assert app["env_file"] == ["/etc/aiflow/secrets/control/aiflow4.env"]
    assert "AIFLOW4_PUBLIC_URL" not in app["environment"]
    assert "Set `AIFLOW4_PUBLIC_URL` in `/etc/aiflow/secrets/control/aiflow4.env`" in (
        root / "docs/v4/jira-connect.md").read_text(encoding="utf-8")


# --- AC1 ---------------------------------------------------------------------------------------------------
def test_connect_redirects_to_consent_with_minimal_scopes_and_state(web, jira_env):
    sign_in(web)
    start = web.post("/v1/integrations/jira/oauth/start", json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert start.status_code == 200
    assert CLIENT_SECRET not in start.text
    url = urlsplit(start.json()["authorize_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{AUTH}/authorize"
    assert set(q["scope"].split()) == {"read:jira-work", "write:jira-work", "offline_access"}
    assert not any("manage" in s or "admin" in s for s in q["scope"].split())
    assert q["audience"] == "api.atlassian.com" and q["response_type"] == "code" and q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "https://control.example.test" + CALLBACK_PATH
    assert len(q["state"]) >= 32
    # the client secret is in the secret store only, readable by its owner only
    path = SecretStore(jira_env.settings.jira_secret_dir).dir / "oauth-app.json"
    assert json.loads(path.read_text())["client_secret"] == CLIENT_SECRET
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    status = web.get("/v1/integrations/jira").json()
    assert status["oauth_app"] == {"configured": True, "client_id": CLIENT_ID} and CLIENT_SECRET not in json.dumps(status)


def test_callback_with_wrong_missing_or_replayed_state_is_refused(web, jira_env, atlassian):
    sign_in(web)
    start = web.post("/v1/integrations/jira/oauth/start", json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    back = urlsplit(atlassian.client.get(start.json()["authorize_url"]).headers["Location"])
    code = parse_qs(back.query)["code"][0]
    assert web.get(f"{CALLBACK_PATH}?code={code}&state=forged-state").status_code == 400
    assert web.get(f"{CALLBACK_PATH}?code={code}").status_code == 400
    # the right state from another browser (no state cookie) is refused too, and a state works only once
    other = TestClient(web.app, base_url="https://control.example.test")
    assert other.get(f"{CALLBACK_PATH}?{back.query}").status_code == 400
    assert web.get(f"{CALLBACK_PATH}?{back.query}").status_code == 400
    assert jira_env.jira_auth.store.credential() is None
    assert "authorization_code" not in atlassian.grants
    refused = [r for r in audit_rows(jira_env, "connect") if not r["accepted"]]
    assert len(refused) == 4 and all("state" in r["result"] for r in refused)


# --- AC2, AC3 ----------------------------------------------------------------------------------------------
def test_oauth_callback_stores_tokens_and_sync_and_writes_use_bearer_via_cloud_id(web, jira_env, atlassian):
    assert not jira_env.settings.jira_email and not jira_env.settings.jira_token  # nothing in owner.env
    sign_in(web)
    done = connect_oauth(web, atlassian)
    assert done.status_code == 303 and done.headers["location"] == "/integrations?jira=connected"
    stored = jira_env.jira_auth.store.credential()
    assert stored["access_token"] in atlassian.access and stored["refresh_token"] in atlassian.refresh
    assert stored["cloud_id"] == CLOUD_ID

    assert jira_sync(jira_env, full=True) == 1
    assert jira_env.db.one("SELECT summary FROM tickets WHERE key='TESTWP-1'")["summary"] == "Synced over the new credential"
    jira_env.jira.transition_to("TESTWP-1", "In Progress")
    jira_env.jira.comment("TESTWP-1", "hello over OAuth")
    jira_env.jira.set_fields("TESTWP-1", {"labels": ["x"]})
    jira_calls = [r for r in atlassian.requests if "/rest/api/3/" in r.url.path and "myself" not in r.url.path]
    assert jira_calls and all(str(r.url).startswith(f"{API}/ex/jira/{CLOUD_ID}/rest/api/3/") for r in jira_calls)
    assert all(r.headers["authorization"] == f"Bearer {stored['access_token']}" for r in jira_calls)
    assert atlassian.status_of["TESTWP-1"] == "In Progress" and atlassian.comments

    status = web.get("/v1/integrations/jira")
    body = status.json()
    assert body["connected"] and body["source"] == "oauth" and body["site_name"] == "acme"
    assert body["cloud_id"] == CLOUD_ID and body["account"] == "bot@acme.test"
    assert set(body["scopes"]) == set(SCOPES) and body["expires_at"]
    for secret in atlassian.issued + [CLIENT_SECRET]:
        assert secret not in status.text and secret not in done.text
    assert [r["accepted"] for r in audit_rows(jira_env, "connect")] == [True]


def test_oauth_callback_rejects_grant_without_refresh_token(web, jira_env, atlassian, caplog):
    atlassian.omit_refresh_for_authorization = True
    sign_in(web)

    response = connect_oauth(web, atlassian)

    assert response.status_code == 303 and response.headers["location"] == "/integrations?jira=failed"
    assert jira_env.jira_auth.store.credential() is None
    status = web.get("/v1/integrations/jira")
    assert status.status_code == 200 and not status.json()["connected"]
    assert not any(request.url.path == "/oauth/token/accessible-resources" for request in atlassian.requests)
    failed = [row for row in audit_rows(jira_env, "connect") if not row["accepted"]]
    assert len(failed) == 1 and "missing refresh_token" in failed[0]["result"]
    for secret in atlassian.issued + [CLIENT_SECRET]:
        assert secret not in response.text and secret not in status.text
        assert secret not in caplog.text and secret not in registry_text(jira_env)


def test_integrations_page_has_connect_disconnect_and_hidden_secret_fields(jira_env):
    from aiflow.v4.control.ui import mount_ui
    app = create_app(jira_env)
    mount_ui(app, jira_env)
    page = TestClient(app).get("/integrations")
    assert page.status_code == 200
    html = page.text
    assert 'data-view="integrations"' in html and "Connect Jira" in html and "Disconnect" in html
    assert "/v1/integrations/jira/oauth/start" in html and "location.assign(d.authorize_url)" in html
    for field in ("client_secret", "api_token"):
        assert f'<input type="password" data-integ-field="{field}"' in html


# --- AC4 ---------------------------------------------------------------------------------------------------
def test_api_token_fallback_is_stored_never_returned_and_used_by_sync(web, jira_env, atlassian):
    sign_in(web)
    email, token = next(iter(atlassian.basic))
    wrong = web.post("/v1/integrations/jira/api-token", json={"email": email, "api_token": "WRONG-TOKEN-x", "site": SITE})
    assert wrong.status_code == 400 and "WRONG-TOKEN-x" not in wrong.text
    assert jira_env.jira_auth.store.credential() is None
    invalid = web.post("/v1/integrations/jira/api-token", json={"api_token": token})
    assert invalid.status_code == 400 and token not in invalid.text

    saved = web.post("/v1/integrations/jira/api-token", json={"email": email, "api_token": token, "site": SITE})
    assert saved.status_code == 200 and token not in saved.text
    assert saved.json()["source"] == "api_token" and saved.json()["account"] == "bot@acme.test"
    assert token not in web.get("/v1/integrations/jira").text
    assert jira_env.jira_auth.store.credential()["api_token"] == token

    assert jira_sync(jira_env, full=True) == 1
    search = [r for r in atlassian.requests if r.url.path == "/rest/api/3/search/jql"][-1]
    assert str(search.url).startswith(SITE) and search.headers["authorization"].startswith("Basic ")
    assert [r["accepted"] for r in audit_rows(jira_env, "connect")] == [False, False, True]
    assert token not in registry_text(jira_env)


# --- AC5 ---------------------------------------------------------------------------------------------------
def test_access_token_is_refreshed_before_it_expires_and_saved(web, jira_env, atlassian):
    sign_in(web)
    connect_oauth(web, atlassian)
    store = jira_env.jira_auth.store
    old = store.credential()
    store.save_credential({**old, "expires_at": int(time.time()) + 60})  # inside the refresh margin

    assert jira_env.jira.status("TESTWP-1") == "To Do"
    new = store.credential()
    assert atlassian.grants.count("refresh_token") == 1
    assert new["access_token"] != old["access_token"] and new["refresh_token"] != old["refresh_token"]
    assert new["expires_at"] > time.time() + 3000
    last = [r for r in atlassian.requests if "/rest/api/3/issue/" in r.url.path][-1]
    assert last.headers["authorization"] == f"Bearer {new['access_token']}"
    assert [r["accepted"] for r in audit_rows(jira_env, "refresh")] == [True]


def test_a_401_from_jira_forces_one_refresh_and_a_retry(web, jira_env, atlassian):
    sign_in(web)
    connect_oauth(web, atlassian)
    atlassian.expire_access()  # Atlassian dropped the access token early; our expiry still says it is valid
    assert jira_env.jira.comment("TESTWP-1", "after a 401") == "10001"
    assert atlassian.grants.count("refresh_token") == 1
    assert jira_env.jira_auth.store.credential()["access_token"] in atlassian.access


# --- AC6 ---------------------------------------------------------------------------------------------------
def test_rejected_refresh_opens_incident_and_asks_for_reconnect(web, jira_env, atlassian):
    sign_in(web)
    connect_oauth(web, atlassian)
    atlassian.refresh.clear()  # the refresh token was revoked on Atlassian's side
    atlassian.expire_access()
    store = jira_env.jira_auth.store
    store.save_credential({**store.credential(), "expires_at": int(time.time()) - 1})

    with pytest.raises(JiraError):
        jira_sync(jira_env, full=True)
    incident = jira_env.db.one("SELECT severity, title FROM incidents WHERE kind='jira_auth' AND closed_at IS NULL")
    assert incident and incident["title"] == "Jira needs to be reconnected"
    status = web.get("/v1/integrations/jira").json()
    assert status["needs_reconnect"] and not status["connected"] and status["site_name"] == "acme"
    assert "access_token" not in store.credential() and "refresh_token" not in store.credential()
    assert [r["accepted"] for r in audit_rows(jira_env, "refresh")] == [False]
    # the loop stops instead of failing on every pass, and refresh is not retried
    assert jira_sync(jira_env) == 0 and not jira_env.jira
    assert atlassian.grants.count("refresh_token") == 1
    assert len(jira_env.db.all("SELECT id FROM incidents WHERE kind='jira_auth'")) == 1
    # reconnecting closes the incident
    connect_oauth(web, atlassian)
    assert not jira_env.db.one("SELECT id FROM incidents WHERE kind='jira_auth' AND closed_at IS NULL")
    assert jira_sync(jira_env, full=True) == 1


# --- AC7 ---------------------------------------------------------------------------------------------------
def test_disconnect_revokes_deletes_and_stops_jira_sync_with_a_warning(web, jira_env, atlassian, caplog):
    jira_env.settings.jira_email, jira_env.settings.jira_token = next(iter(atlassian.basic))  # even with owner.env
    sign_in(web)
    connect_oauth(web, atlassian)
    refresh = jira_env.jira_auth.store.credential()["refresh_token"]

    out = web.post("/v1/integrations/jira/disconnect")
    assert out.status_code == 200 and out.json()["revoked"] is True and out.json()["state"] == "disconnected"
    assert atlassian.revoked == [refresh]
    assert jira_env.jira_auth.store.credential() is None
    assert not (SecretStore(jira_env.settings.jira_secret_dir).dir / "credential.json").exists()

    before = len(atlassian.requests)
    with caplog.at_level(logging.WARNING, logger="aiflow4.jira_connect"):
        assert jira_sync(jira_env, full=True) == 0
        assert jira_sync(jira_env, full=True) == 0
    assert len(atlassian.requests) == before and not jira_env.jira
    assert [r.getMessage() for r in caplog.records].count("jira_sync stopped: Jira is disconnected") == 1
    incident = jira_env.db.one("SELECT title FROM incidents WHERE kind='jira_auth' AND closed_at IS NULL")
    assert incident["title"] == "Jira is disconnected"
    assert [r["accepted"] for r in audit_rows(jira_env, "disconnect")] == [True]


# --- AC8 ---------------------------------------------------------------------------------------------------
def test_owner_env_token_keeps_working_until_a_ui_credential_takes_priority(web, jira_env, atlassian):
    email, token = next(iter(atlassian.basic))
    jira_env.settings.jira_email, jira_env.settings.jira_token = email, token
    assert jira_sync(jira_env, full=True) == 1
    search = [r for r in atlassian.requests if r.url.path == "/rest/api/3/search/jql"][-1]
    assert str(search.url).startswith(SITE) and search.headers["authorization"].startswith("Basic ")
    sign_in(web)
    assert web.get("/v1/integrations/jira").json()["source"] == "owner_env"

    connect_oauth(web, atlassian)
    assert jira_sync(jira_env, full=True) == 1
    search = [r for r in atlassian.requests if r.url.path.endswith("/rest/api/3/search/jql")][-1]
    assert str(search.url).startswith(f"{API}/ex/jira/{CLOUD_ID}/") and search.headers["authorization"].startswith("Bearer ")


def test_empty_control_repo_project_set_does_not_fall_back_to_jira_projects(web, jira_env, atlassian):
    sign_in(web)
    connect_oauth(web, atlassian)
    jira_env.settings.jira_projects = ["LEGACY"]
    jira_env.repo().projects.clear()
    before = len(atlassian.requests)

    assert jira_sync(jira_env, full=True) == 0
    assert len(atlassian.requests) == before


def test_without_secret_store_wiring_the_gateway_still_uses_fixed_basic_auth(atlassian):
    email, token = next(iter(atlassian.basic))
    jira = Jira(SITE, email, token, client=httpx.Client(transport=httpx.MockTransport(atlassian.handle), base_url=SITE,
                                                        auth=(email, token)))
    assert jira and jira.status("TESTWP-1") == "To Do"


# --- AC9 ---------------------------------------------------------------------------------------------------
def test_only_the_owner_may_connect_store_tokens_or_disconnect(web, jira_env, atlassian):
    sign_in(web)
    connect_oauth(web, atlassian)
    before = jira_env.jira_auth.store.credential()
    email, token = next(iter(atlassian.basic))
    calls = [("post", "/v1/integrations/jira/oauth/start", {"client_id": "evil", "client_secret": "evil-secret"}),
             ("post", "/v1/integrations/jira/api-token", {"email": email, "api_token": token, "site": SITE}),
             ("post", "/v1/integrations/jira/disconnect", {}), ("get", "/v1/integrations/jira", None)]
    anonymous = TestClient(web.app, base_url="https://control.example.test")
    lead = TestClient(web.app, base_url="https://control.example.test")
    sign_in(lead, role="lead", subject="lead@example.test")
    wrong_bearer = {"Authorization": "Bearer not-the-owner"}
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(anonymous, method)(path, **kwargs).status_code == 401
        assert getattr(anonymous, method)(path, headers=wrong_bearer, **kwargs).status_code == 401
        assert getattr(lead, method)(path, **kwargs).status_code == 403
    assert jira_env.jira_auth.store.credential() == before
    assert jira_env.jira_auth.store.app()["client_id"] == CLIENT_ID
    refused = [r for r in audit_rows(jira_env) if not r["accepted"]]
    assert len(refused) == 9 and {r["actor"] for r in refused} == {"", "lead@example.test"}
    # the owner bearer token is accepted as well (same token as the other owner actions)
    owner = TestClient(web.app, base_url="https://control.example.test")
    assert owner.get("/v1/integrations/jira", headers={"Authorization": "Bearer owner-secret"}).status_code == 200


# --- AC10 --------------------------------------------------------------------------------------------------
def test_every_connect_refresh_failure_and_disconnect_is_audited_without_tokens(web, jira_env, atlassian, caplog):
    caplog.set_level(logging.DEBUG)
    sign_in(web)
    email, token = next(iter(atlassian.basic))
    responses = [connect_oauth(web, atlassian)]
    store = jira_env.jira_auth.store
    store.save_credential({**store.credential(), "expires_at": int(time.time())})
    jira_sync(jira_env, full=True)  # refresh ok
    atlassian.refresh.clear()
    store.save_credential({**store.credential(), "expires_at": int(time.time())})
    with pytest.raises(JiraError):
        jira_sync(jira_env, full=True)  # refresh rejected
    responses.append(web.get("/v1/integrations/jira"))
    responses.append(web.post("/v1/integrations/jira/api-token", json={"email": email, "api_token": token, "site": SITE}))
    responses.append(web.post("/v1/integrations/jira/disconnect"))
    responses.append(web.get("/v1/integrations/jira"))

    actions = [(r["action"], r["accepted"]) for r in audit_rows(jira_env)]
    assert actions == [("oauth_app", True), ("connect", True), ("refresh", True), ("refresh", False),
                       ("connect", True), ("disconnect", True)]
    secrets = atlassian.issued + [CLIENT_SECRET, token]
    text = registry_text(jira_env)
    logs = caplog.text
    for secret in secrets:
        assert secret not in text, "a token reached the registry"
        assert secret not in logs, "a token reached the log"
        assert all(secret not in r.text for r in responses), "a token reached an API response"
