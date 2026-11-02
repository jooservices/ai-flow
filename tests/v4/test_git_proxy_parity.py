"""Characterization of `github_git_proxy` before it is split into smaller named functions (JAIF-532).

Every row of the tables below is an observed result of the unchanged handler. The same tests must pass again,
unchanged, after the extraction.
"""
from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from conftest import add_ticket, join_node
from test_github_connections import TOKEN, PersonalGitHub, add_token
from test_projects import owner_sign_in

from aiflow.v4.control import gateway_helpers
from aiflow.v4.control.db import J
from aiflow.v4.control.features.github_fenced.routes import (
    _authorize_check_probe, _authorize_run_transfer, _authorized_git_transfer, _git_transfer_request)
from aiflow.v4.control.features.projects import routes as project_routes
from aiflow.v4.control.github_connections import GitHubConnections
from aiflow.v4.control.gateway_helpers import _issue_git_proxy_token


def _basic(token: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()}


def _packet(command: str) -> bytes:
    payload = command.encode() + b"\0report-status\n"
    return f"{len(payload) + 4:04x}".encode() + payload + b"0000"


@pytest.fixture
def proxy(client, ctx, tmp_path, monkeypatch):
    """A token connection, a check-probe node and one active dev run, plus signed capabilities for each branch."""
    ctx.settings.github_app_dir = str(tmp_path / "github-secrets")
    ctx.github.org = "jooservices"
    ctx.github.meta = {"id": "901", "name": "AI Flow", "account_login": "jooservices",
                       "account_type": "Organization", "installation_id": 77}
    ctx.github_registry = GitHubConnections(ctx.settings.github_app_dir, lambda d, o: type(ctx.github)(),
                                            lambda token, account, kind: PersonalGitHub(token, account))
    monkeypatch.setattr(project_routes, "verify_token",
                        lambda token, account="": PersonalGitHub(token, account or "person"))
    owner_sign_in(client, ctx)
    connection = add_token(client).json()["id"]
    gateway_helpers.github_connections(ctx).assign("person/tool", connection)
    ctx.repo().projects["TESTWP"].repositories = [
        {"repo": "person/tool", "base": "develop", "checks": ["pytest"]}]
    join_node(client, "cc")

    key = "TESTWP-532"
    run_id = str(uuid.uuid4())
    add_ticket(ctx.db, key)
    claim = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
    assert claim.ok
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,details)"
               " VALUES (%s,%s,'TESTWP','dev','codex-01','codex','node1',%s,'running',%s)",
               (run_id, key, claim.fence, J({"repository": "person/tool"})))
    expires = 2_000_000_000

    def mint(**claims):
        return _issue_git_proxy_token(ctx, {
            "connection": connection, "repo": "person/tool", "expires": expires, **claims})

    credentials = {
        "check": mint(purpose="check_probe", node="cc", project="TESTWP"),
        "check_other": mint(purpose="check_probe", repo="person/other", node="cc", project="TESTWP"),
        "run": mint(run_id=run_id, fence=claim.fence, node="node1", branch="TESTWP-532-work"),
        "run_without_branch": mint(run_id=run_id, fence=claim.fence, node="node1"),
        "run_wrong_node": mint(run_id=run_id, fence=claim.fence, node="node2", branch="TESTWP-532-work"),
        "run_wrong_fence": mint(run_id=run_id, fence=claim.fence + 1, node="node1", branch="TESTWP-532-work"),
        "run_other_repo": mint(run_id=run_id, fence=claim.fence, node="node1", repo="person/other",
                               branch="TESTWP-532-work"),
        "expired": mint(purpose="check_probe", node="cc", project="TESTWP", expires=1),
        "bad_purpose": mint(purpose="other", node="cc", project="TESTWP"),
        "default_app": mint(connection="default", repo="jooservices/test-aiflow-wp", purpose="check_probe",
                            node="cc", project="TESTWP"),
        "unknown_connection": mint(connection="missing", repo="person/tool", purpose="check_probe",
                                   node="cc", project="TESTWP"),
        "bad_signature": "not.a.token",
    }
    return SimpleNamespace(ctx=ctx, client=client, connection=connection, run_id=run_id, key=key,
                           fence=claim.fence, credentials=credentials)


REFUSALS = [
    # name, credential, method, plot, query, expected status, expected detail
    ("missing credential", None, "GET", "person/tool.git/info/refs", {"service": "git-upload-pack"}, 401,
     "Git repository transfer credential is invalid or expired"),
    ("bad signature", "bad_signature", "GET", "person/tool.git/info/refs", {"service": "git-upload-pack"}, 401,
     "Git repository transfer credential is invalid or expired"),
    ("expired credential", "expired", "GET", "person/tool.git/info/refs", {"service": "git-upload-pack"}, 401,
     "Git repository transfer credential is invalid or expired"),
    ("no .git separator", "check", "GET", "person/tool", {"service": "git-upload-pack"}, 403,
     "Git repository transfer is outside this credential's scope"),
    ("repository has no owner", "check", "GET", "person.git/info/refs", {"service": "git-upload-pack"}, 403,
     "Git repository transfer is outside this credential's scope"),
    ("too many path segments", "check", "GET", "person/tool/extra.git/info/refs",
     {"service": "git-upload-pack"}, 403, "Git repository transfer is outside this credential's scope"),
    ("other repository", "check", "GET", "person/other.git/info/refs", {"service": "git-upload-pack"}, 403,
     "Git repository transfer is outside this credential's scope"),
    ("info refs without service", "check", "GET", "person/tool.git/info/refs", {}, 400, "unsupported Git service"),
    ("unsupported service", "check", "GET", "person/tool.git/info/refs", {"service": "git-archive"}, 400,
     "unsupported Git service"),
    ("unsupported GET path", "check", "GET", "person/tool.git/HEAD", {}, 404,
     "unsupported Git repository transfer operation"),
    ("info refs over POST", "check", "POST", "person/tool.git/info/refs", {"service": "git-upload-pack"}, 404,
     "unsupported Git repository transfer operation"),
    ("check probe receive-pack refs", "check", "GET", "person/tool.git/info/refs",
     {"service": "git-receive-pack"}, 403, "check-probe Git credentials are read-only"),
    ("check probe receive-pack POST", "check", "POST", "person/tool.git/git-receive-pack", None, 403,
     "check-probe Git credentials are read-only"),
    ("check probe repository removed", "check_other", "GET", "person/other.git/info/refs",
     {"service": "git-upload-pack"}, 409, "check-probe repository is no longer configured"),
    ("unsupported purpose", "bad_purpose", "GET", "person/tool.git/info/refs", {"service": "git-upload-pack"},
     403, "Git repository transfer credential has an unsupported purpose"),
    ("run on another node", "run_wrong_node", "GET", "person/tool.git/info/refs",
     {"service": "git-upload-pack"}, 409, "Git repository transfer run is no longer active"),
    ("run with another fence", "run_wrong_fence", "GET", "person/tool.git/info/refs",
     {"service": "git-upload-pack"}, 409, "Git repository transfer run is no longer active"),
    ("run for another repository", "run_other_repo", "GET", "person/other.git/info/refs",
     {"service": "git-upload-pack"}, 409, "Git repository transfer run is no longer active"),
    ("run without a branch pushes", "run_without_branch", "GET", "person/tool.git/info/refs",
     {"service": "git-receive-pack"}, 403, "Git push is not allowed by this read-only credential"),
    ("access-token connection required", "default_app", "GET", "jooservices/test-aiflow-wp.git/info/refs",
     {"service": "git-upload-pack"}, 403,
     "Git relay credentials are only valid for access-token connections"),
    ("unknown connection", "unknown_connection", "GET", "person/tool.git/info/refs",
     {"service": "git-upload-pack"}, 409,
     "the GitHub connection assigned to person/tool cannot reach it"),
]


@pytest.mark.parametrize("name, credential, method, path, query, status, detail", REFUSALS,
                         ids=[case[0] for case in REFUSALS])
def test_git_proxy_refusals(proxy, name, credential, method, path, query, status, detail):
    headers = _basic(proxy.credentials[credential]) if credential else {}
    if method == "GET":
        response = proxy.client.get(f"/v1/github/git/{path}", params=query, headers=headers)
    else:
        response = proxy.client.post(f"/v1/github/git/{path}", params=query, headers=headers)
    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_git_proxy_refusal_asks_for_basic_credentials(proxy):
    response = proxy.client.get("/v1/github/git/person/tool.git/info/refs",
                                params={"service": "git-upload-pack"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="AI Flow Git transfer"'


def test_git_proxy_run_without_a_lease_is_refused(proxy):
    proxy.ctx.leases.release(proxy.key, proxy.run_id, proxy.fence)
    response = proxy.client.get("/v1/github/git/person/tool.git/info/refs",
                                params={"service": "git-upload-pack"},
                                headers=_basic(proxy.credentials["run"]))
    assert response.status_code == 409
    assert response.json() == {"detail": "Git repository transfer run is no longer active"}


@pytest.mark.parametrize("case, path, method, query, body, status, content", [
    ("check probe clone", "person/tool.git/info/refs", "GET", {"service": "git-upload-pack"}, None, 200, b"pack"),
    ("check probe upload-pack", "person/tool.git/git-upload-pack", "POST", None, b"want\n", 200, b"pack"),
    ("uppercase repository", "Person/Tool.git/info/refs", "GET", {"service": "git-upload-pack"}, None, 200, b"pack"),
    ("run clone", "person/tool.git/info/refs", "GET", {"service": "git-upload-pack"}, None, 200, b"pack"),
], ids=["check-probe-clone", "check-probe-upload-pack", "uppercase-repository", "run-clone"])
def test_git_proxy_forwards_read_operations(proxy, monkeypatch, case, path, method, query, body, status, content):
    calls = []

    def relay(repository, suffix, method_, params, headers, request_body, stored_token):
        calls.append((repository, suffix, method_, params, request_body, stored_token))
        return httpx.Response(200, headers={
            "Content-Type": "application/x-git-upload-pack-result", "Cache-Control": "no-cache",
            "Expires": "0", "Pragma": "no-cache", "X-Not-Forwarded": "dropped"}, content=b"pack")

    monkeypatch.setattr(gateway_helpers, "_github_git_upstream", relay)
    credential = "run" if case == "run clone" else "check"
    if method == "GET":
        response = proxy.client.get(f"/v1/github/git/{path}", params=query,
                                    headers=_basic(proxy.credentials[credential]))
    else:
        response = proxy.client.post(f"/v1/github/git/{path}", content=body,
                                     headers=_basic(proxy.credentials[credential]))
    assert response.status_code == status and response.content == content
    assert calls == [(("person/tool" if case != "uppercase repository" else "Person/Tool"),
                      ".git/info/refs" if suffix(path) == ".git/info/refs" else ".git/git-upload-pack",
                      method, [] if not query else list(query.items()), b"" if body is None else body, TOKEN)]
    assert response.headers["content-type"] == "application/x-git-upload-pack-result"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert response.headers["pragma"] == "no-cache"
    assert "x-not-forwarded" not in response.headers


def suffix(path: str) -> str:
    _, separator, rest = path.partition(".git")
    assert separator
    return ".git" + rest


@pytest.mark.parametrize("case, body, upstream_status", [
    ("accepted push", _packet("0" * 40 + " " + "1" * 40 + " refs/heads/TESTWP-532-work"), 200),
    ("rejected push", _packet("0" * 40 + " " + "1" * 40 + " refs/heads/OTHER-1-work"), 403),
    ("malformed push", b"not-a-pkt-line", 403),
], ids=["accepted-push", "wrong-branch", "malformed-pack"])
def test_git_proxy_push_targets_are_limited_to_the_run_branch(proxy, monkeypatch, case, body, upstream_status):
    calls = []

    def relay(repository, suffix_, method, params, headers, request_body, stored_token):
        calls.append((repository, suffix_, method, request_body, stored_token))
        return httpx.Response(200, headers={"Content-Type": "application/x-git-receive-pack-result"}, content=b"ok")

    monkeypatch.setattr(gateway_helpers, "_github_git_upstream", relay)
    response = proxy.client.post("/v1/github/git/person/tool.git/git-receive-pack", content=body,
                                 headers=_basic(proxy.credentials["run"]))
    if upstream_status == 403:
        assert response.status_code == 403
        assert response.json() == {"detail": "Git push is limited to the branch assigned to this run"}
        assert calls == []
        return
    assert response.status_code == 200 and response.content == b"ok"
    assert calls == [("person/tool", ".git/git-receive-pack", "POST", body, TOKEN)]
    event = proxy.ctx.db.one("SELECT type,aggregate,aggregate_id,payload FROM domain_events"
                             " WHERE type='github.push_accepted' ORDER BY seq DESC LIMIT 1")
    assert event == {"type": "github.push_accepted", "aggregate": "run", "aggregate_id": proxy.run_id,
                     "payload": {"run_id": proxy.run_id, "fence": proxy.fence, "ticket": proxy.key,
                                 "actor": "codex-01", "repo": "person/tool", "branch": "TESTWP-532-work"}}
    audit = proxy.ctx.db.one("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload"
                             " FROM write_audit WHERE target='github' AND action='git-push'"
                             " ORDER BY id DESC LIMIT 1")
    assert audit == {"target": "github", "action": "git-push", "ticket": proxy.key, "run_id": proxy.run_id,
                     "fence": proxy.fence, "actor": "codex-01", "accepted": True, "result": "pushed",
                     "payload": {"repo": "person/tool", "branch": "TESTWP-532-work",
                                 "connection": proxy.connection}}


def test_git_proxy_audits_a_rejected_push(proxy, monkeypatch):
    def relay(repository, suffix_, method, params, headers, request_body, stored_token):
        return httpx.Response(500, headers={"Content-Type": "text/plain"}, content=b"no")

    monkeypatch.setattr(gateway_helpers, "_github_git_upstream", relay)
    body = _packet("0" * 40 + " " + "1" * 40 + " refs/heads/TESTWP-532-work")
    response = proxy.client.post("/v1/github/git/person/tool.git/git-receive-pack", content=body,
                                 headers=_basic(proxy.credentials["run"]))
    assert response.status_code == 500 and response.content == b"no"
    audit = proxy.ctx.db.one("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload"
                             " FROM write_audit WHERE target='github' AND action='git-push'"
                             " ORDER BY id DESC LIMIT 1")
    assert audit == {"target": "github", "action": "git-push", "ticket": proxy.key, "run_id": proxy.run_id,
                     "fence": proxy.fence, "actor": "codex-01", "accepted": False,
                     "result": "GitHub returned 500",
                     "payload": {"repo": "person/tool", "branch": "TESTWP-532-work",
                                 "connection": proxy.connection}}
    assert proxy.ctx.db.one("SELECT type FROM domain_events WHERE type='github.push_accepted'") is None


def test_git_proxy_reports_an_upstream_transport_failure(proxy, monkeypatch, caplog):
    def relay(*args, **kwargs):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(gateway_helpers, "_github_git_upstream", relay)
    response = proxy.client.get("/v1/github/git/person/tool.git/info/refs",
                                params={"service": "git-upload-pack"},
                                headers=_basic(proxy.credentials["check"]))
    assert response.status_code == 502
    assert response.json() == {"detail": "GitHub repository transfer failed"}
    assert "GitHub repository transfer failed (ConnectError)" in caplog.text


# --- the extracted steps, without services ---------------------------------------------------------------


def _request(method: str, **params):
    return SimpleNamespace(method=method, query_params=params)


def _project(*repos: str):
    return SimpleNamespace(configured_repositories=list(repos))


def test_git_transfer_request_resolves_operation_and_scope():
    capability = {"repo": "person/tool"}
    assert _git_transfer_request("person/tool.git/info/refs", _request("GET", service="git-upload-pack"),
                                 capability) == ("person/tool", ".git/info/refs", "git-upload-pack")
    assert _git_transfer_request("person/tool.git/git-receive-pack", _request("POST"),
                                 capability) == ("person/tool", ".git/git-receive-pack", "git-receive-pack")
    with pytest.raises(HTTPException) as failed:
        _git_transfer_request("person/other.git/info/refs", _request("GET", service="git-upload-pack"),
                              capability)
    assert (failed.value.status_code, failed.value.detail) == (
        403, "Git repository transfer is outside this credential's scope")
    with pytest.raises(HTTPException) as failed:
        _git_transfer_request("person/tool.git/info/refs", _request("GET", service="git-archive"), capability)
    assert (failed.value.status_code, failed.value.detail) == (400, "unsupported Git service")
    with pytest.raises(HTTPException) as failed:
        _git_transfer_request("person/tool.git/HEAD", _request("GET"), capability)
    assert (failed.value.status_code, failed.value.detail) == (
        404, "unsupported Git repository transfer operation")


def test_authorize_check_probe_is_read_only_and_scoped():
    fleet = SimpleNamespace(probe_project=lambda node, project: _project("person/tool"))
    assert _authorize_check_probe({"node": "cc", "project": "TESTWP"}, "person/tool", "git-upload-pack",
                                  fleet) == (None, "", -1)
    with pytest.raises(HTTPException) as failed:
        _authorize_check_probe({"node": "cc", "project": "TESTWP"}, "person/tool", "git-receive-pack", fleet)
    assert (failed.value.status_code, failed.value.detail) == (403, "check-probe Git credentials are read-only")
    with pytest.raises(HTTPException) as failed:
        _authorize_check_probe({"node": "cc", "project": "TESTWP"}, "person/other", "git-upload-pack", fleet)
    assert (failed.value.status_code, failed.value.detail) == (
        409, "check-probe repository is no longer configured")


def test_authorize_run_transfer_requires_a_live_fenced_run():
    run = {"node": "node1", "fence": 3, "ticket_key": "TESTWP-532", "project": "TESTWP"}
    ctx = SimpleNamespace(leases=SimpleNamespace(valid=lambda key, run_id, fence: True),
                          repo=lambda: SimpleNamespace(projects={"TESTWP": object()}))
    options = dict(run_row=lambda run_id: run, matches_run_repository=lambda repository, requested: True,
                   repository_for_run=lambda row, project: "person/tool")
    capability = {"run_id": "run-1", "fence": 3, "node": "node1", "branch": "TESTWP-532-work"}
    assert _authorize_run_transfer(capability, "person/tool", "git-receive-pack", ctx, **options) == (
        run, "run-1", 3)
    with pytest.raises(HTTPException) as failed:
        _authorize_run_transfer({**capability, "node": "node2"}, "person/tool", "git-receive-pack", ctx, **options)
    assert (failed.value.status_code, failed.value.detail) == (
        409, "Git repository transfer run is no longer active")
    with pytest.raises(HTTPException) as failed:
        _authorize_run_transfer({**capability, "branch": ""}, "person/tool", "git-receive-pack", ctx, **options)
    assert (failed.value.status_code, failed.value.detail) == (
        403, "Git push is not allowed by this read-only credential")


def test_authorized_git_transfer_rejects_an_unknown_purpose():
    with pytest.raises(HTTPException) as failed:
        _authorized_git_transfer({"purpose": "other"}, "person/tool", "git-upload-pack", ctx=None,
                                 fleet_routes=None, run_row=None, matches_run_repository=None,
                                 repository_for_run=None)
    assert (failed.value.status_code, failed.value.detail) == (
        403, "Git repository transfer credential has an unsupported purpose")
