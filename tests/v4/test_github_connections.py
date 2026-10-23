"""Several GitHub connections held by Command Center, chosen per repository (JAIF-220)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from conftest import FakeGitHub, add_ticket, join_node
from test_projects import owner_sign_in, prepare_project_control, worker_headers
from test_reviews import add_review_run
import aiflow.v4.control.api as api
from aiflow.v4.control.api import github_gateway
from aiflow.v4.control.github_connections import GitHubConnections
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.node import probes

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

    def token(self, repo=None, permissions=None):
        from datetime import datetime, timedelta, timezone
        return self._token_value, datetime.now(timezone.utc) + timedelta(hours=1)


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
    with pytest.raises(HTTPException) as refused:
        github_gateway(ctx, "jooservices/not-reachable")
    assert refused.value.status_code == 409


def test_short_invalid_token_is_never_echoed_by_validation_response(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    secret = "short-credential-sentinel"
    response = add_token(client, token=secret[:8])
    assert response.status_code == 422
    assert secret[:8] not in response.text


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


def test_review_rejection_comments_and_closes_pr_through_the_assigned_connection(client, ctx, secrets_dir):
    owner_sign_in(client, ctx)
    connection = add_token(client).json()["id"]
    api.github_connections(ctx).assign("person/tool", connection)
    gateway = github_gateway(ctx, "person/tool")
    gateway.pr_by_number[42] = {"number": 42, "state": "open", "merged": False}

    key = "TESTWP-220"
    add_ticket(ctx.db, key, status="Human review", labels=("require-human",))
    add_review_run(ctx, key, pr_url="https://github.com/person/tool/pull/42",
                   details={"repository": "person/tool"})
    response = client.post(f"/v1/reviews/{key}/reject", json={"reason": "Out of scope."},
                           headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200, response.text
    assert gateway.comments and gateway.comments[-1][0] == 42
    assert gateway.pr_by_number[42]["state"] == "closed"
    assert not getattr(ctx.github, "comments", []) and not ctx.github.pr_by_number
    writes = ctx.db.all("SELECT action,payload FROM write_audit WHERE target='github' AND ticket=%s"
                        " AND action IN ('comment','close') ORDER BY id", (key,))
    assert [row["action"] for row in writes] == ["comment", "close"]
    assert all(row["payload"]["connection"] == connection for row in writes)


def test_check_probe_uses_assigned_connection_and_relays_token_credentials_read_only(
        client, ctx, secrets_dir, monkeypatch):
    owner_sign_in(client, ctx)
    connection = add_token(client).json()["id"]
    project = ctx.repo().projects["TESTWP"]
    project.repositories = [{"repo": "person/tool", "base": "develop", "checks": ["pytest"]}]
    api.github_connections(ctx).assign("person/tool", connection)
    node = join_node(client, "cc")
    assert node.status_code == 200
    headers = {"Authorization": f"Bearer {node.json()['node_token']}"}

    issued = client.post("/v1/nodes/cc/check-probes/token",
                         json={"project": "TESTWP", "repo": "person/tool"}, headers=headers)
    assert issued.status_code == 200, issued.text
    credential = issued.json()
    assert credential["token"] != TOKEN
    assert credential["transport_url"].endswith("/v1/github/git/person/tool.git")
    assert ctx.db.one("SELECT payload FROM write_audit WHERE action='check_probe_token' ORDER BY id DESC LIMIT 1"
                      )["payload"]["connection"] == connection

    upstream_calls = []

    def relay(repository, suffix, method, params, proxy_headers, content, stored_token):
        upstream_calls.append((repository, suffix, method, stored_token))
        import httpx
        return httpx.Response(200, headers={"Content-Type": "application/x-git-upload-pack-result"}, content=b"pack")

    monkeypatch.setattr(api, "_github_git_upstream", relay)
    basic = "Basic " + base64.b64encode(f"x-access-token:{credential['token']}".encode()).decode()
    clone = client.get("/v1/github/git/person/tool.git/info/refs", params={"service": "git-upload-pack"},
                       headers={"Authorization": basic})
    assert clone.status_code == 200 and clone.content == b"pack"
    assert upstream_calls == [("person/tool", ".git/info/refs", "GET", TOKEN)]
    push_refs = client.get("/v1/github/git/person/tool.git/info/refs", params={"service": "git-receive-pack"},
                           headers={"Authorization": basic})
    assert push_refs.status_code == 403


def test_node_check_probe_clones_from_the_command_center_relay(tmp_path, monkeypatch):
    calls = []

    class FakeGit:
        def __init__(self, root, run_as=None):
            self.root = root

        def mkdirs(self, path):
            Path(path).mkdir(parents=True, exist_ok=True)

        def run(self, *args, **kwargs):
            calls.append((args, kwargs))

    class API:
        def call(self, method, path, body):
            assert method == "POST" and path.endswith("/check-probes/token")
            assert body == {"project": "TESTWP", "repo": "person/tool"}
            return {"token": "opaque-relay-capability", "transport_url":
                    "https://control.example.test/v1/github/git/person/tool.git"}

    monkeypatch.setattr(probes, "Git", FakeGit)
    monkeypatch.setattr(probes, "probe_command", lambda command, cwd, env, run_as:
                        {"command": command, "status": "ran", "detail": "exit 0"})
    results = probes.run_check_probe(API(), "cc", {"project": "TESTWP", "repositories": [
        {"repo": "person/tool", "base": "develop", "checks": ["pytest"]}]}, str(tmp_path), [])

    assert results == [{"repo": "person/tool", "command": "pytest", "status": "ran", "detail": "exit 0"}]
    clone_args, clone_kwargs = calls[0]
    assert "https://control.example.test/v1/github/git/person/tool.git" in clone_args
    assert clone_kwargs["token"] == "opaque-relay-capability"


def test_routes_projects_and_github_operations_after_removing_the_default_app(
        client, ctx, control_repo, secrets_dir):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    connection = add_token(client).json()["id"]
    config_path = control_repo / "projects/TESTWP.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["repo"] = "person/tool"
    config["repositories"] = [{"repo": "person/tool", "base": "develop", "checks": []}]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    api.github_connections(ctx).assign("person/tool", connection)

    removed = client.delete("/v1/integrations/github/connections/default")
    assert removed.status_code == 200, removed.text
    assert ctx.github is None
    catalog = client.get("/v1/projects/catalog")
    assert catalog.status_code == 200, catalog.text
    assert {row["full_name"] for row in catalog.json()["github_repos"]} == {"person/tool"}
    repositories = client.get("/v1/projects/TESTWP/repositories")
    assert repositories.status_code == 200, repositories.text
    assert repositories.json()["repositories"][0]["connection"] == connection

    headers = worker_headers(client)
    add_ticket(ctx.db, "TESTWP-221")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-221", "account": "codex-01", "step": "dev"},
                          headers=headers)
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    run = {"run_id": claimed.json()["run_id"], "fence": claimed.json()["fence"]}
    token = client.post("/v1/github/token", json={**run, "repo": "person/tool"}, headers=headers)
    assert token.status_code == 200, token.text
    assert token.json()["transport_url"].endswith("/v1/github/git/person/tool.git")
    assert token.json()["token"] != TOKEN
    pr = client.post("/v1/github/pr", json={**run, "repo": "person/tool", "head": "TESTWP-221-work",
                                            "base": "develop", "title": "Alternate connection", "body": "",
                                            "draft": True}, headers=headers)
    assert pr.status_code == 200, pr.text
    assert ctx.github is None
    assert github_gateway(ctx, "person/tool").prs[-1]["repo"] == "tool"
    write = ctx.db.one("SELECT payload FROM write_audit WHERE target='github' AND action='pr'"
                       " AND run_id=%s", (run["run_id"],))
    assert write["payload"]["connection"] == connection


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


def test_projects_editor_recovers_a_stale_assignment_with_the_only_reachable_connection(
        client, ctx, control_repo, secrets_dir):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    connection = add_token(client).json()["id"]
    ctx.jira.search_data = []
    config_path = control_repo / "projects/TESTWP.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["repo"] = "person/tool"
    config["repositories"] = [{"repo": "person/tool", "base": "develop", "checks": []}]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    api.github_connections(ctx).assign("person/tool", "default")  # the previous App no longer reaches it

    loaded = client.get("/v1/projects/TESTWP/repositories")
    assert loaded.status_code == 200, loaded.text
    repo = loaded.json()["repositories"][0]
    assert repo["assigned_connection"] == "default" and repo["connection_stale"] is True
    assert repo["connection"] == connection and repo["connections"] == [connection]
    catalog_repo = next(item for item in loaded.json()["github_repos"] if item["full_name"] == "person/tool")
    assert catalog_repo["assigned_connection"] == "default" and catalog_repo["connection"] == ""

    saved = client.put("/v1/projects/TESTWP/repositories", json={"repositories": [{
        "repo": repo["repo"], "base": repo["base"], "checks": repo["checks"], "stack": repo["stack"],
        "connection": repo["connection"],  # exactly the connection shown in the editor
    }], "epic_repositories": {}})
    assert saved.status_code == 200, saved.text
    assert api.github_connections(ctx).assignments()["person/tool"] == connection


def test_commands_use_assigned_token_connection_through_repository_transfer_relay_and_audit_writes(
        client, ctx, control_repo, secrets_dir, monkeypatch):
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
    assert token.status_code == 200, token.text
    credential = token.json()
    assert credential["token"] != TOKEN and credential["transport_url"].endswith("/v1/github/git/person/tool.git")
    assert TOKEN not in token.text
    assert (secrets_dir / "worker-git-proxy.key").stat().st_mode & 0o777 == 0o600

    upstream_calls = []

    def relay(repository, suffix, method, params, proxy_headers, content, stored_token):
        upstream_calls.append((repository, suffix, method, params, proxy_headers, content, stored_token))
        import httpx
        return httpx.Response(200, headers={"Content-Type": "application/x-git-upload-pack-result"}, content=b"pack")

    monkeypatch.setattr(api, "_github_git_upstream", relay)
    basic = lambda value: {"Authorization": "Basic " + base64.b64encode(
        f"x-access-token:{value}".encode()).decode()}
    clone = client.get("/v1/github/git/person/tool.git/info/refs", params={"service": "git-upload-pack"},
                       headers=basic(credential["token"]))
    assert clone.status_code == 200 and clone.content == b"pack"
    assert upstream_calls[-1][0:3] == ("person/tool", ".git/info/refs", "GET")
    assert upstream_calls[-1][-1] == TOKEN  # raw access token is used only by the control plane relay

    branch = "TESTWP-51-work"
    push_token = client.post("/v1/github/token", json={**run, "repo": "person/tool", "branch": branch},
                             headers=headers).json()
    denied_service = client.get("/v1/github/git/person/tool.git/info/refs",
                                params={"service": "git-receive-pack"}, headers=basic(credential["token"]))
    assert denied_service.status_code == 403
    command = f"{'0' * 40} {'1' * 40} refs/heads/{branch}\0report-status\n".encode()
    pack_request = f"{len(command) + 4:04x}".encode() + command + b"0000"
    wrong_branch_command = f"{'0' * 40} {'1' * 40} refs/heads/TESTWP-52-wrong\0report-status\n".encode()
    denied_push = client.post("/v1/github/git/person/tool.git/git-receive-pack",
                              content=f"{len(wrong_branch_command) + 4:04x}".encode()
                              + wrong_branch_command + b"0000",
                              headers={**basic(push_token["token"]),
                                       "Content-Type": "application/x-git-receive-pack-request"})
    assert denied_push.status_code == 403
    pushed = client.post("/v1/github/git/person/tool.git/git-receive-pack", content=pack_request,
                         headers={**basic(push_token["token"]),
                                  "Content-Type": "application/x-git-receive-pack-request"})
    assert pushed.status_code == 200
    assert upstream_calls[-1][1:3] == (".git/git-receive-pack", "POST")
    assert ctx.db.one("SELECT payload FROM write_audit WHERE action='git-push' ORDER BY id DESC LIMIT 1")[
        "payload"]["connection"] == conn

    out_of_scope = client.get("/v1/github/git/person/not-the-repo.git/info/refs",
                              params={"service": "git-upload-pack"}, headers=basic(credential["token"]))
    assert out_of_scope.status_code == 403

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
