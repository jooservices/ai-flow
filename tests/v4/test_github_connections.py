"""Several GitHub connections held by Command Center, chosen per repository (JAIF-220)."""
from __future__ import annotations

import json

import pytest
import yaml
from fastapi import HTTPException

from conftest import FakeGitHub, add_ticket
from test_projects import owner_sign_in, prepare_project_control, worker_headers
import aiflow.v4.control.api as api
from aiflow.v4.control.api import github_gateway
from aiflow.v4.control.github_connections import GitHubConnections
from aiflow.v4.core.controlrepo import ControlRepo

TOKEN = "github_pat_" + "p" * 40


class PersonalGitHub(FakeGitHub):
    """The personal account behind an access token: same call surface, never hands out a token."""
    kind = "token"
    org = "person"

    def __init__(self, token: str = TOKEN, account: str = "person") -> None:
        super().__init__()
        self._token_value = token
        self.org = account
        self.meta = {"id": "", "account_login": account, "account_type": "User"}
        self.repositories = [{"full_name": "person/tool", "name": "tool", "default_branch": "develop"},
                             {"full_name": "someone-else/skipped", "name": "skipped", "default_branch": "main"}]


@pytest.fixture
def secrets_dir(ctx, tmp_path, monkeypatch):
    ctx.settings.github_app_dir = str(tmp_path / "github-secrets")
    ctx.github.org = "jooservices"
    ctx.github.meta = {"id": "901", "name": "AI Flow", "account_login": "jooservices", "account_type": "Organization"}
    ctx.github_registry = GitHubConnections(ctx.settings.github_app_dir, lambda d, o: FakeGitHub(),
                                            lambda token, account, kind: PersonalGitHub(token, account))
    monkeypatch.setattr(api, "verify_token", lambda token, account="": PersonalGitHub(token, account or "person"))
    return tmp_path / "github-secrets"


def add_token(client, **extra):
    return client.post("/v1/integrations/github/connections/token", json={"token": TOKEN, **extra})


def test_single_app_installation_is_the_first_connection(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    listed = client.get("/v1/integrations/github/connections").json()
    assert [c["id"] for c in listed["connections"]] == ["default"]
    first = listed["connections"][0]
    assert first["kind"] == "app" and first["account"] == "jooservices" and first["status"] == "connected"
    assert "jooservices/new-app" in first["repositories"]
    assert github_gateway(ctx, "jooservices/new-app") is ctx.github  # nothing else configured: unchanged path


def test_token_is_verified_stored_in_secret_store_and_never_shown_again(client, ctx, secrets_dir, caplog):
    owner_sign_in(client, ctx)
    added = add_token(client, label="Personal")
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["kind"] == "token" and body["account"] == "person" and body["label"] == "Personal"
    assert body["repositories"] == ["person/tool"]  # only repositories of the connection's account
    assert "recommend" in body["warning"].lower() or "prefer a github app" in body["warning"].lower()
    stored = secrets_dir / "connections" / body["id"] / "token"
    assert stored.read_text() == TOKEN and stored.stat().st_mode & 0o777 == 0o600

    listed = client.get("/v1/integrations/github/connections")
    assert [c["id"] for c in listed.json()["connections"]] == ["default", body["id"]]
    audit = json.dumps(ctx.db.all("SELECT action, result, payload FROM write_audit WHERE target='github'"))
    events = json.dumps(ctx.db.all("SELECT * FROM events"), default=str)
    for text in (added.text, listed.text, audit, events, caplog.text):
        assert TOKEN not in text
    assert ctx.db.one("SELECT payload FROM write_audit WHERE action='add_token_connection'")["payload"][
        "connection"] == body["id"]


def test_token_rejected_by_github_is_not_stored(client, ctx, secrets_dir, monkeypatch):
    owner_sign_in(client, ctx)

    def refuse(token, account=""):
        raise RuntimeError("401 bad credentials " + token)

    monkeypatch.setattr(api, "verify_token", refuse)
    response = add_token(client)
    assert response.status_code == 422 and TOKEN not in response.text
    assert not (secrets_dir / "connections").exists()


def test_connection_management_is_owner_only(client, ctx, secrets_dir):
    ctx.settings.console_session_secret = "connections-console-session-secret-32-bytes"
    assert add_token(client).status_code == 401
    assert client.get("/v1/integrations/github/connections").status_code == 401


def test_test_replace_and_remove_a_token_connection(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    conn = add_token(client).json()["id"]
    tested = client.post(f"/v1/integrations/github/connections/{conn}/test")
    assert tested.status_code == 200 and tested.json()["status"] == "connected"
    new_token = "github_pat_" + "n" * 40
    replaced = client.put(f"/v1/integrations/github/connections/{conn}/token", json={"token": new_token})
    assert replaced.status_code == 200 and new_token not in replaced.text
    assert (secrets_dir / "connections" / conn / "token").read_text() == new_token
    assert client.put("/v1/integrations/github/connections/default/token", json={"token": new_token}).status_code == 409
    removed = client.delete(f"/v1/integrations/github/connections/{conn}")
    assert removed.status_code == 200 and not (secrets_dir / "connections" / conn).exists()
    assert client.post(f"/v1/integrations/github/connections/{conn}/test").status_code == 404
    actions = {r["action"] for r in ctx.db.all("SELECT action FROM write_audit WHERE target='github'")}
    assert {"add_token_connection", "test_connection", "replace_token_connection", "remove_connection"} <= actions


def test_repository_uses_its_own_connection_and_unreachable_repository_is_refused(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    conn = add_token(client).json()["id"]
    assert github_gateway(ctx, "person/tool").org == "person"          # the owner's account has its token
    assert github_gateway(ctx, "jooservices/new-app") is ctx.github     # the organization keeps the App
    for stranger in ("someone-else/skipped", "person/not-listed", "jooservices/not-listed"):
        with pytest.raises(HTTPException) as refused:
            github_gateway(ctx, stranger)
        assert refused.value.status_code == 409 and "can reach" in refused.value.detail
    registry = api.github_connections(ctx)
    registry.assign("person/tool", "default")  # assigned connection does not reach it: refused, not guessed
    with pytest.raises(HTTPException) as refused:
        github_gateway(ctx, "person/tool")
    assert refused.value.status_code == 409
    registry.assign("person/tool", conn)
    assert github_gateway(ctx, "person/tool").org == "person"


def test_projects_screen_shows_and_changes_the_connection_of_each_repository(client, ctx, control_repo, secrets_dir):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    conn = add_token(client).json()["id"]
    ctx.jira.search_data = []

    loaded = client.get("/v1/projects/TESTWP/repositories").json()
    assert [c["id"] for c in loaded["connections"]] == ["default", conn]
    by_repo = {r["full_name"]: r for r in loaded["github_repos"]}
    assert by_repo["person/tool"]["connection"] == conn and by_repo["jooservices/new-app"]["connection"] == "default"
    assert loaded["repositories"][0]["connection"] == "default"

    unreachable = client.put("/v1/projects/TESTWP/repositories", json={"repositories": [
        {"repo": "person/tool", "connection": "default"}], "epic_repositories": {}})
    assert unreachable.status_code == 422 and "cannot reach" in unreachable.text
    assert api.github_connections(ctx).assignments() == {}

    saved = client.put("/v1/projects/TESTWP/repositories", json={"repositories": [
        {"repo": "jooservices/test-aiflow-wp", "connection": "default"},
        {"repo": "person/tool", "connection": conn}], "epic_repositories": {}})
    assert saved.status_code == 200, saved.text
    assert api.github_connections(ctx).assignments() == {"jooservices/test-aiflow-wp": "default", "person/tool": conn}
    config = yaml.safe_load((control_repo / "projects/TESTWP.yaml").read_text())
    assert "connection" not in json.dumps(config)  # the control repo never learns about credentials
    reloaded = client.get("/v1/projects/TESTWP/repositories").json()
    assert {r["repo"]: r["connection"] for r in reloaded["repositories"]}["person/tool"] == conn
    assert client.delete(f"/v1/integrations/github/connections/{conn}").status_code == 409  # still assigned


def test_commands_use_the_assigned_connection_nodes_get_no_token_for_access_tokens_and_audit_names_it(
        client, ctx, control_repo, secrets_dir):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    conn = add_token(client).json()["id"]
    config_path = control_repo / "projects/TESTWP.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["repositories"] = [{"repo": "person/tool", "base": "develop", "checks": ["pytest"], "stack": ""}]
    config["epic_repositories"] = {"TESTWP-50": "person/tool"}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    ctx.repo = lambda: ControlRepo.load(control_repo)
    headers = worker_headers(client)
    ctx.db.run("INSERT INTO tickets (key,project,status,type,labels,summary,description,parent_key)"
               " VALUES ('TESTWP-50','TESTWP','In Progress','Epic',%s,'Core','',NULL)", ([],))
    add_ticket(ctx.db, "TESTWP-51")
    ctx.db.run("UPDATE tickets SET type='Story',parent_key='TESTWP-50' WHERE key='TESTWP-51'")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-51", "account": "codex-01", "step": "dev"},
                          headers=headers).json()
    run = {"run_id": claimed["run_id"], "fence": claimed["fence"]}

    token = client.post("/v1/github/token", json={**run, "repo": "person/tool"}, headers=headers)
    assert token.status_code == 409 and TOKEN not in token.text and "token" not in token.json().get("token", "")

    pr = client.post("/v1/github/pr", json={**run, "repo": "person/tool", "head": "TESTWP-51-work",
                                            "base": "develop", "title": "t", "body": "", "draft": True},
                     headers=headers)
    assert pr.status_code == 200, pr.text
    comment = client.post("/v1/github/comment", json={**run, "number": 1, "body": "hi"}, headers=headers)
    assert comment.status_code == 200, comment.text
    rows = ctx.db.all("SELECT action, payload FROM write_audit WHERE target='github' AND run_id=%s ORDER BY id",
                      (claimed["run_id"],))
    assert [r["action"] for r in rows if r["action"] in ("pr", "comment")] == ["pr", "comment"]
    assert all(r["payload"]["connection"] == conn for r in rows)
    assert TOKEN not in json.dumps(rows)
    assert ctx.github.prs == []  # the organization App never touched the personal repository


def test_new_app_connection_is_added_beside_the_existing_one(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    registry = api.github_connections(ctx)
    conn = registry.save_app(None, "Second App", {"id": "55", "slug": "second", "name": "Second",
                                                    "account_login": "acme", "account_type": "Organization",
                                                    "installation_id": 9}, "-----BEGIN RSA PRIVATE KEY-----\nx\n")
    listed = client.get("/v1/integrations/github/connections").json()["connections"]
    assert [c["id"] for c in listed] == ["default", conn]
    assert listed[1]["kind"] == "app" and listed[1]["account"] == "acme" and listed[1]["label"] == "Second App"
    assert (secrets_dir / "connections" / conn / "app.pem").stat().st_mode & 0o777 == 0o600


def test_second_github_app_is_imported_beside_the_first_and_both_repositories_are_listed(client, ctx, tmp_path,
                                                                                         monkeypatch):
    from test_github_connect import PRIVATE_KEY, owner_sign_in as connect_sign_in, patch_flow_gateway
    patch_flow_gateway(monkeypatch)
    ctx.github = None
    ctx.settings.github_app_dir = str(tmp_path / "apps")
    connect_sign_in(client, ctx)

    def import_app(connection, installation):
        staged = client.post("/v1/integrations/github/imports",
                             json={"app_id": 901, "private_key": PRIVATE_KEY, "connection": connection})
        assert staged.status_code == 200, staged.text
        done = client.post(f"/v1/integrations/github/imports/{staged.json()['pending_id']}/connect",
                           json={"installation_id": installation})
        assert done.status_code == 200, done.text
        return done.json()["connection"]

    assert import_app("new", 77) == "default"  # nothing connected yet: the first App is the first connection
    second = import_app("new", 88)
    assert second != "default"
    listed = client.get("/v1/integrations/github/connections").json()["connections"]
    assert [(c["id"], c["account"], c["kind"]) for c in listed] == [("default", "acme", "app"), (second, "person", "app")]
    assert listed[1]["repositories"] == ["person/private"]
    assert PRIVATE_KEY not in json.dumps(listed)
    catalog = client.get("/v1/projects/catalog").json()["github_repos"]
    assert {r["full_name"]: r["connection"] for r in catalog} == {"acme/first": "default", "person/private": second}
    assert github_gateway(ctx, "person/private").org == "person"
    with pytest.raises(HTTPException):
        github_gateway(ctx, "acme/missing")
    ctx.settings.public_url = "https://control.example.test"
    assert client.get("/v1/integrations/github/manifest/start?organization=acme&connection=bogus"
                      ).status_code == 422
