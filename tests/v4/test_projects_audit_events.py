"""Successful projects GitHub and environment writes publish events for the audit ledger."""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from conftest import FakeGitHub, add_ticket, join_node
from aiflow.v4.control import route_deps
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.features.projects import routes as project_routes
from aiflow.v4.control.github_connections import GitHubConnections
from aiflow.v4.core import secretbox
from aiflow.v4.core.controlrepo import ControlRepo


ACTOR = "owner@example.test"
SESSION_SECRET = "projects-events-console-session-secret-over-32-bytes"
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\n" + ("x" * 160) + "\n-----END RSA PRIVATE KEY-----\n"
TOKEN = "github_pat_" + "p" * 40
PERMISSIONS = {"contents": "write", "pull_requests": "write", "workflows": "write", "actions": "read",
               "checks": "read", "statuses": "read", "metadata": "read", "administration": "write"}


@pytest.fixture(autouse=True)
def clear_github_connection_context(monkeypatch):
    monkeypatch.setenv("AIFLOW4_SECRET_KEY", secretbox.generate_key())
    route_deps.github_connection_used.set("")


def owner_sign_in(client, ctx):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session(ACTOR, "owner", 2_000_000_000, SESSION_SECRET))


class FakePersonalGitHub(FakeGitHub):
    def __init__(self, token: str = TOKEN, account: str = "person") -> None:
        super().__init__()
        self._token_value = token
        self.org = account
        self.meta = {"id": "", "account_login": account, "account_type": "User"}
        self.repositories = [{"full_name": f"{account}/tool", "name": "tool", "default_branch": "develop"}]


class FakeInstalledApp(FakeGitHub):
    details = {"id": 901, "slug": "aiflow-connect", "name": "AI Flow Connect",
               "html_url": "https://github.com/apps/aiflow-connect", "permissions": PERMISSIONS}
    repositories_by_account = {
        "acme": [{"full_name": "acme/first", "name": "first", "default_branch": "develop"}],
    }

    def __init__(self, app_dir, org, client=None):
        super().__init__()
        self.meta = json.loads((Path(app_dir) / "app.json").read_text(encoding="utf-8"))
        self.org = self.meta.get("account_login") or org
        self._inst = self.meta.get("installation_id")
        self.repositories = list(self.repositories_by_account.get(self.org, []))

    def app_details(self):
        return dict(self.details)

    def installation_details(self, installation_id):
        assert installation_id == 77
        return {"id": installation_id, "account": {"login": "acme", "type": "Organization"},
                "suspended_at": None}

    def installation_repositories(self):
        return list(self.repositories)


@pytest.fixture
def secrets_dir(ctx, tmp_path, monkeypatch):
    ctx.settings.github_app_dir = str(tmp_path / "github-secrets")
    ctx.github.org = "jooservices"
    ctx.github.meta = {"id": "901", "name": "AI Flow", "account_login": "jooservices",
                       "account_type": "Organization", "installation_id": 77}
    ctx.github_registry = GitHubConnections(ctx.settings.github_app_dir,
                                            lambda directory, org: FakeInstalledApp(directory, org),
                                            lambda token, account, kind: FakePersonalGitHub(token, account))
    monkeypatch.setattr(project_routes, "verify_token",
                        lambda token, account="": FakePersonalGitHub(token, account or "person"))
    monkeypatch.setattr(project_routes, "GitHubApp", FakeInstalledApp)
    return Path(ctx.settings.github_app_dir)


def assert_event_and_audit(db, event_type, *, event_payload, aggregate="github", aggregate_id="integration",
                           target="github", action, ticket="", run_id="", fence=None, actor=ACTOR, result, payload):
    events = db.all("SELECT seq,type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s ORDER BY seq",
                    (event_type,))
    assert len(events) == 1
    assert {key: value for key, value in events[0].items() if key != "seq"} == {
        "type": event_type, "aggregate": aggregate, "aggregate_id": aggregate_id, "payload": event_payload}
    rows = db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                  "WHERE target=%s AND action=%s", (target, action))
    assert rows == [{"target": target, "action": action, "ticket": ticket, "run_id": run_id, "fence": fence,
                     "actor": actor, "accepted": True, "result": result, "payload": payload}]


def integration_payload(actor=ACTOR, **fields):
    return {"actor": actor, **fields, "scope": "integration"}


def test_repository_snapshot_and_access_change_publish_events(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "app.json").write_text(json.dumps(ctx.github.meta), encoding="utf-8")
    (secrets_dir / "app.pem").write_text("test key", encoding="utf-8")

    snapshot = client.get("/v1/integrations/github")
    assert snapshot.status_code == 200, snapshot.text
    assert_event_and_audit(
        ctx.db, "github.repository_snapshot_recorded",
        event_payload=integration_payload(count=3, source="integration_refresh"),
        action="repository_snapshot", result="GitHub installation repositories recorded",
        payload={"count": 3, "source": "integration_refresh", "connection": "default"})

    ctx.github.repositories = [ctx.github.repositories[0],
                               {"full_name": "jooservices/new-repository", "name": "new-repository",
                                "default_branch": "main"}]
    changed = client.get("/v1/integrations/github")
    assert changed.status_code == 200, changed.text
    assert_event_and_audit(
        ctx.db, "github.repository_access_changed",
        event_payload=integration_payload(added=["jooservices/new-repository"],
                                          removed=["jooservices/aiflow-control", "jooservices/new-app"],
                                          source="integration_refresh"),
        action="repositories_changed", result="GitHub installation repository access changed",
        payload={"added": ["jooservices/new-repository"],
                 "removed": ["jooservices/aiflow-control", "jooservices/new-app"],
                 "source": "integration_refresh", "connection": "default"})


def test_token_connection_add_replace_and_remove_publish_events(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    added = client.post("/v1/integrations/github/connections/token", json={"token": TOKEN, "label": "Personal"})
    assert added.status_code == 200, added.text
    connection = added.json()["id"]
    assert_event_and_audit(ctx.db, "github.token_connection_added",
                           event_payload=integration_payload(connection=connection, account="person"),
                           action="add_token_connection", result="access token connection added",
                           payload={"connection": connection, "account": "person"})

    replacement = "github_pat_" + "n" * 40
    replaced = client.put(f"/v1/integrations/github/connections/{connection}/token", json={"token": replacement})
    assert replaced.status_code == 200, replaced.text
    assert_event_and_audit(ctx.db, "github.token_connection_replaced",
                           event_payload=integration_payload(connection=connection, account="person"),
                           action="replace_token_connection", result="access token replaced",
                           payload={"connection": connection, "account": "person"})

    removed = client.delete(f"/v1/integrations/github/connections/{connection}")
    assert removed.status_code == 200, removed.text
    assert_event_and_audit(ctx.db, "github.connection_removed",
                           event_payload=integration_payload(connection=connection),
                           action="remove_connection", result="connection removed",
                           payload={"connection": connection})


def test_manifest_start_and_creation_publish_events(client, ctx, secrets_dir, monkeypatch):
    owner_sign_in(client, ctx)
    ctx.settings.public_url = "https://control.example.test"

    start = client.get("/v1/integrations/github/manifest/start?organization=acme")
    assert start.status_code == 200, start.text
    state = re.search(r"\?state=([a-f0-9]{64})", start.text).group(1)
    assert_event_and_audit(ctx.db, "github.manifest_started",
                           event_payload=integration_payload(organization="acme"), action="manifest_start",
                           result="GitHub App manifest started", payload={"organization": "acme"})

    class ConversionResponse:
        status_code = 201

        @staticmethod
        def json():
            return {**FakeInstalledApp.details, "owner": {"login": "acme", "type": "Organization"},
                    "pem": PRIVATE_KEY}

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

    monkeypatch.setattr(httpx, "Client", ConversionClient)
    callback = client.get(f"/v1/integrations/github/manifest/callback?code=oneTimeCode&state={state}",
                          follow_redirects=False)
    assert callback.status_code == 303
    assert_event_and_audit(ctx.db, "github.manifest_created",
                           event_payload=integration_payload(app_id="901", organization="acme"),
                           action="manifest_created", result="GitHub App created",
                           payload={"app_id": "901", "organization": "acme"})


def test_import_install_connect_and_settings_publish_events(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    imported = client.post("/v1/integrations/github/imports", json={"app_id": 901, "private_key": PRIVATE_KEY})
    assert imported.status_code == 200, imported.text
    pending_id = imported.json()["pending_id"]
    assert_event_and_audit(ctx.db, "github.app_import_verified", event_payload=integration_payload(app_id=901),
                           action="import_app", result="GitHub App credentials verified", payload={"app_id": 901})

    opened = client.get(f"/v1/integrations/github/imports/{pending_id}/install", follow_redirects=False)
    assert opened.status_code == 303
    assert_event_and_audit(ctx.db, "github.app_install_opened", event_payload=integration_payload(app_id="901"),
                           action="open_app_install", result="GitHub App installation opened",
                           payload={"app_id": "901"})

    connected = client.post(f"/v1/integrations/github/imports/{pending_id}/connect", json={"installation_id": 77})
    assert connected.status_code == 200, connected.text
    assert_event_and_audit(
        ctx.db, "github.app_connected",
        event_payload=integration_payload(app_id="901", installation_id=77, account="acme", repositories=1,
                                          source="import", connection="default"),
        action="connect", result="GitHub App connected",
        payload={"app_id": "901", "installation_id": 77, "account": "acme", "repositories": 1,
                 "source": "import", "connection": "default"})

    settings = client.get("/v1/integrations/github/installation-settings", follow_redirects=False)
    assert settings.status_code == 303
    assert_event_and_audit(
        ctx.db, "github.installation_settings_opened",
        event_payload=integration_payload(app_id="901", installation_id=77, account="acme"),
        action="open_installation_settings", result="GitHub repository settings opened",
        payload={"app_id": "901", "installation_id": 77, "account": "acme", "connection": "default"})


def prepare_project_control(ctx, control_repo: Path):
    from aiflow.v4.control import inventory

    (control_repo / "versions.lock").write_text(
        "schema: 1\ntools:\n  claude_code: 2.1.305\n  python3: '3.12'\n  uv: '0.8'\n", encoding="utf-8")
    ctx.settings.control_repo = str(control_repo)
    ctx.repo = lambda: inventory.apply(ControlRepo.load(control_repo), ctx.db)

    def pull_fake_commit():
        if not ctx.github.commits:
            return
        commit = ctx.github.commits[-1]
        path = control_repo / commit["path"]
        if commit.get("deleted"):
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(commit["content"], encoding="utf-8")

    ctx.refresh_repo = pull_fake_commit


def test_assign_connection_publishes_event_and_same_audit(client, ctx, secrets_dir, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    token_connection = client.post("/v1/integrations/github/connections/token", json={"token": TOKEN})
    assert token_connection.status_code == 200, token_connection.text
    connection = token_connection.json()["id"]

    saved = client.put("/v1/projects/TESTWP/repositories", json={
        "repositories": [{"repo": "person/tool", "base": "develop", "connection": connection}],
        "epic_repositories": {},
    })
    assert saved.status_code == 200, saved.text
    assert_event_and_audit(
        ctx.db, "github.connection_assigned",
        event_payload=integration_payload(repo="person/tool", connection=connection, project="TESTWP"),
        action="assign_connection", result=f"person/tool uses connection {connection}",
        payload={"repo": "person/tool", "connection": connection, "project": "TESTWP"})


def test_environment_removals_publish_fenced_event_and_same_audit(client, ctx, db):
    ticket = "TESTWP-478"
    add_ticket(ctx.db, ticket, status="Intake", labels=())
    run_id = str(uuid4())
    lease = ctx.leases.claim(ticket, run_id, ctx.settings.lease_ms)
    assert lease.ok
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,token_hash,details)"
               " VALUES (%s,%s,'TESTWP','intake','grok-01','grok','', 'cc',%s,'running','','{}')",
               (run_id, ticket, lease.fence))
    joined = join_node(client, "cc")
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}

    response = client.post("/v1/environment/removals", headers=headers,
                           json={"run_id": run_id, "fence": lease.fence, "removed": ["obsolete wording"], "needs": []})
    assert response.status_code == 200, response.text
    assert_event_and_audit(
        ctx.db, "environment.removals_recorded",
        event_payload={"run_id": run_id, "fence": lease.fence, "ticket": ticket, "actor": "grok-01",
                       "removed": 1, "tickets": 0, "needs": 0},
        aggregate="run", aggregate_id=run_id, target="environment", action="removals", ticket=ticket,
        run_id=run_id, fence=lease.fence, actor="grok-01", result="1 removed, 0 environment ticket(s)",
        payload={"needs": 0})
