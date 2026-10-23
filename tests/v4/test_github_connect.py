from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session


SESSION_SECRET = "github-connect-console-session-secret-over-32-bytes"
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\n" + ("x" * 160) + "\n-----END RSA PRIVATE KEY-----\n"
REQUIRED_PERMISSIONS = {"contents": "write", "pull_requests": "write", "workflows": "write",
                        "actions": "read", "checks": "read", "statuses": "read", "metadata": "read",
                        "administration": "write"}


class FakeConnectGitHub:
    details = {"id": 901, "slug": "aiflow-connect", "name": "AI Flow Connect", "html_url": "https://github.com/apps/aiflow-connect",
               "owner": {"login": "app-owner", "type": "Organization"}, "permissions": REQUIRED_PERMISSIONS}
    installations = [
        {"id": 77, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "selected"},
        {"id": 88, "account": {"login": "person", "type": "User"}, "repository_selection": "all"},
    ]
    repos_by_account = {
        "acme": [{"full_name": "acme/first", "name": "first", "default_branch": "main"}],
        "person": [{"full_name": "person/private", "name": "private", "default_branch": "main"}],
    }

    def __init__(self, app_dir, org, client=None):
        self.app_dir = app_dir
        self.meta = json.loads(open(os.path.join(app_dir, "app.json"), encoding="utf-8").read())
        self.key = open(os.path.join(app_dir, "app.pem"), encoding="utf-8").read()
        self.org = self.meta.get("account_login") or org
        self._inst = self.meta.get("installation_id")
        self.repositories_by_account = {key: list(rows) for key, rows in self.repos_by_account.items()}

    def app_details(self):
        return dict(self.details)

    def app_installations(self):
        return list(self.installations)

    def installation_details(self, installation_id):
        row = next((item for item in self.installations if item["id"] == installation_id), None)
        if row is None:
            raise ValueError("unknown installation")
        return {"id": row["id"], "account": row["account"], "suspended_at": None}

    def installation_repositories(self):
        return list(self.repositories_by_account.get(self.org, []))

    def token(self, repo=None, permissions=None):
        return "ghs_short_lived_test_token", datetime.now(timezone.utc) + timedelta(minutes=55)


def owner_sign_in(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set(COOKIE_NAME, issue_session("owner@example.test", role, 2_000_000_000, SESSION_SECRET))


def patch_flow_gateway(monkeypatch):
    import aiflow.v4.control.api as api
    monkeypatch.setattr(api, "GitHubApp", FakeConnectGitHub)
    return api


def test_existing_app_import_verifies_installation_persists_only_private_key_and_refreshes_repo_catalog(
        client, ctx, tmp_path, monkeypatch, caplog):
    patch_flow_gateway(monkeypatch)
    ctx.settings.github_app_dir = str(tmp_path / "github-secrets")
    owner_sign_in(client, ctx)

    staged = client.post("/v1/integrations/github/imports", json={"app_id": 901, "private_key": PRIVATE_KEY})
    assert staged.status_code == 200
    imported = staged.json()
    assert PRIVATE_KEY not in staged.text
    assert "ghs_" not in staged.text
    assert imported["app"] == {"id": "901", "name": "AI Flow Connect", "slug": "aiflow-connect"}

    options = client.get(f"/v1/integrations/github/imports/{imported['pending_id']}/installations")
    assert options.status_code == 200
    assert options.json()["installations"] == [
        {"id": 77, "login": "acme", "type": "Organization", "repository_selection": "selected"},
        {"id": 88, "login": "person", "type": "User", "repository_selection": "all"},
    ]
    opened = client.get(f"/v1/integrations/github/imports/{imported['pending_id']}/install", follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == "https://github.com/apps/aiflow-connect/installations/new"

    connected = client.post(f"/v1/integrations/github/imports/{imported['pending_id']}/connect",
                            json={"installation_id": 77})
    assert connected.status_code == 200, connected.text
    assert connected.json()["account"] == "acme"
    assert connected.json()["installation_id"] == 77

    secret_dir = tmp_path / "github-secrets"
    saved = json.loads((secret_dir / "app.json").read_text())
    assert saved == {"id": "901", "slug": "aiflow-connect", "name": "AI Flow Connect",
                     "html_url": "https://github.com/apps/aiflow-connect", "account_login": "acme",
                     "account_type": "Organization", "installation_id": 77,
                     "repository_names": ["acme/first"]}
    assert (secret_dir / "app.pem").read_text() == PRIVATE_KEY
    assert (secret_dir / "app.pem").stat().st_mode & 0o777 == 0o600

    status = client.get("/v1/integrations/github")
    assert status.status_code == 200 and status.json()["connected"]
    assert status.json()["account"] == {"login": "acme", "type": "Organization"}
    assert PRIVATE_KEY not in status.text and "ghs_" not in status.text

    owner_sign_in(client, ctx, role="super_admin")
    admin_status = client.get("/v1/integrations/github")
    assert admin_status.status_code == 200 and admin_status.json()["connected"]

    catalog = client.get("/v1/projects/catalog")
    assert catalog.status_code == 200
    assert [row["full_name"] for row in catalog.json()["github_repos"]] == ["acme/first"]
    ctx.github.repositories_by_account["acme"].append({"full_name": "acme/added-later", "name": "added-later",
                                                      "default_branch": "main"})
    refreshed = client.get("/v1/projects/catalog")
    assert "acme/added-later" in {row["full_name"] for row in refreshed.json()["github_repos"]}

    settings = client.get("/v1/integrations/github/installation-settings", follow_redirects=False)
    assert settings.status_code == 303
    assert settings.headers["location"] == "https://github.com/organizations/acme/settings/installations/77"
    audit = ctx.db.all("SELECT action, result, payload FROM write_audit WHERE target='github' ORDER BY id")
    encoded_audit = json.dumps(audit)
    assert "import_app" in {row["action"] for row in audit}
    assert "connect" in {row["action"] for row in audit}
    assert "repositories_changed" in {row["action"] for row in audit}
    assert "open_installation_settings" in {row["action"] for row in audit}
    assert PRIVATE_KEY not in encoded_audit and "ghs_" not in encoded_audit
    assert PRIVATE_KEY not in caplog.text and "ghs_" not in caplog.text


def test_new_app_manifest_round_trip_verifies_installation_and_audits_without_credentials(
        client, ctx, tmp_path, monkeypatch, caplog):
    api = patch_flow_gateway(monkeypatch)
    ctx.settings.github_app_dir = str(tmp_path / "github-secrets")
    ctx.settings.public_url = "https://control.example.test"
    owner_sign_in(client, ctx)

    class ConversionResponse:
        status_code = 201

        @staticmethod
        def json():
            return {**FakeConnectGitHub.details, "owner": {"login": "acme", "type": "Organization"},
                    "pem": PRIVATE_KEY, "client_secret": "client-secret-never-save",
                    "webhook_secret": "hook-secret-never-save"}

    class ConversionClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path):
            assert path == "/app-manifests/oneTimeCode/conversions"
            return ConversionResponse()

    monkeypatch.setattr(api.httpx, "Client", ConversionClient)
    start = client.get("/v1/integrations/github/manifest/start?organization=acme")
    assert start.status_code == 200
    assert "https://github.com/organizations/acme/settings/apps/new" in start.text
    assert '&quot;contents&quot;:&quot;write&quot;' in start.text
    assert '&quot;pull_requests&quot;:&quot;write&quot;' in start.text
    assert '&quot;actions&quot;:&quot;read&quot;' in start.text
    assert '&quot;administration&quot;:&quot;write&quot;' in start.text
    state = re.search(r"\?state=([a-f0-9]{64})", start.text).group(1)
    callback = client.get(f"/v1/integrations/github/manifest/callback?code=oneTimeCode&state={state}",
                          follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"] == "https://github.com/apps/aiflow-connect/installations/new"
    setup = client.get(f"/v1/integrations/github/setup?installation_id=77&state={state}&setup_action=install",
                       follow_redirects=False)
    assert setup.status_code == 303 and setup.headers["location"] == "/integrations?github=connected"

    secret_dir = tmp_path / "github-secrets"
    saved = json.loads((secret_dir / "app.json").read_text())
    assert saved["account_login"] == "acme" and saved["installation_id"] == 77
    assert "client_secret" not in saved and "webhook_secret" not in saved
    assert (secret_dir / "app.pem").read_text() == PRIVATE_KEY
    audit = ctx.db.all("SELECT action, result, payload FROM write_audit WHERE target='github' ORDER BY id")
    assert {row["action"] for row in audit} >= {"manifest_start", "manifest_created", "connect"}
    encoded_audit = json.dumps(audit)
    assert PRIVATE_KEY not in encoded_audit and "oneTimeCode" not in encoded_audit
    assert "client-secret-never-save" not in encoded_audit and "hook-secret-never-save" not in encoded_audit
    assert PRIVATE_KEY not in caplog.text


def test_github_app_must_have_actions_read_for_ci_job_logs():
    from aiflow.v4.control.api import validate_github_app_permissions

    validate_github_app_permissions({"permissions": REQUIRED_PERMISSIONS})
    missing_actions = dict(REQUIRED_PERMISSIONS)
    del missing_actions["actions"]
    with pytest.raises(ValueError, match="permissions AI Flow requires"):
        validate_github_app_permissions({"permissions": missing_actions})


def test_github_connect_is_owner_only_and_manifest_requires_public_https_url(client, ctx, tmp_path):
    ctx.settings.github_app_dir = str(tmp_path / "github-secrets")
    ctx.settings.console_session_secret = SESSION_SECRET
    assert client.get("/v1/integrations/github").status_code == 401
    owner_sign_in(client, ctx, role="lead")
    assert client.get("/v1/integrations/github").status_code == 403
    owner_sign_in(client, ctx)
    response = client.get("/v1/integrations/github/manifest/start?organization=acme")
    assert response.status_code == 503
    assert "AIFLOW4_PUBLIC_URL" in response.text


def test_github_and_jira_connect_docs_configure_public_url_in_control_env_file():
    github_doc = re.sub(r"\s+", " ", Path("docs/v4/github-connect.md").read_text())
    jira_doc = re.sub(r"\s+", " ", Path("docs/v4/jira-connect.md").read_text())
    env_file = "`/etc/aiflow/secrets/control/aiflow4.env`"

    assert "Set `AIFLOW4_PUBLIC_URL`" in github_doc and f"in {env_file}" in github_doc
    assert "Set `AIFLOW4_PUBLIC_URL`" in jira_doc and f"in {env_file}" in jira_doc
    assert "compose file passes this value through" not in github_doc.lower()
    assert "environment used by `docker compose`" not in github_doc


def test_integrations_ui_and_repo_refresh_controls_are_present(client, ctx):
    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.ui import mount_ui
    app = create_app(ctx)
    mount_ui(app, ctx)
    from fastapi.testclient import TestClient
    page = TestClient(app).get("/integrations")
    assert page.status_code == 200
    assert 'id="tab-integrations"' in page.text and 'id="view-integrations"' in page.text
    assert "Connect GitHub" in page.text and "Verify existing App" in page.text
    assert "data-github-key-file" in page.text and "private_key =" not in page.text
    assert "/v1/integrations/github/installation-settings" in page.text
    assert "Refresh repositories" in page.text


def test_github_gateway_uses_stored_installation_and_mints_short_lived_repo_tokens(tmp_path):
    import time

    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from aiflow.v4.control.github import GitHubApp

    app_dir = tmp_path / "github-app"
    app_dir.mkdir()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    (app_dir / "app.json").write_text(json.dumps({"id": "901", "slug": "app", "account_login": "acme",
                                                  "installation_id": 77}))
    (app_dir / "app.pem").write_text(pem)
    calls = []

    def handler(request):
        calls.append(request)
        auth = request.headers.get("authorization", "")
        if request.url.path.startswith("/app/installations/") and request.url.path.endswith("/access_tokens"):
            assert auth.startswith("Bearer ey")
            payload = json.loads(request.content)
            if "repositories" in payload:
                assert payload["repositories"] == ["repo"]
            return httpx.Response(201, json={"token": f"ghs_short_{len(calls)}",
                                              "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()})
        if request.url.path == "/installation/repositories":
            assert auth.startswith("Bearer ghs_short_")
            return httpx.Response(200, json={"repositories": [{"full_name": "acme/repo", "name": "repo",
                                                                  "default_branch": "main"}]})
        return httpx.Response(404, json={"message": "not found"})

    gateway = GitHubApp(str(app_dir), "old-owner", httpx.Client(base_url="https://api.github.com",
                                                                  transport=httpx.MockTransport(handler)))
    token, expires = gateway.token("acme/repo", {"contents": "write", "pull_requests": "write", "metadata": "read"})
    assert token.startswith("ghs_short_")
    assert timedelta(minutes=54) < expires - datetime.now(timezone.utc) < timedelta(minutes=56)
    assert gateway.token("acme/repo", {"contents": "write", "pull_requests": "write", "metadata": "read"})[0] == token
    assert len(calls) == 1
    assert gateway.installation_repositories()[0]["full_name"] == "acme/repo"

    time.sleep(0.002)
    (app_dir / "app.json").write_text(json.dumps({"id": "901", "slug": "app", "account_login": "new-owner",
                                                  "installation_id": 88}))
    new_token, _ = gateway.token("acme/repo", {"contents": "write", "pull_requests": "write", "metadata": "read"})
    assert new_token.startswith("ghs_short_") and gateway.org == "new-owner"
    assert any("/app/installations/88/access_tokens" in request.url.path for request in calls)

