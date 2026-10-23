"""Owner-managed project configuration and lifecycle through the console API."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from conftest import add_ticket, join_node
from aiflow.v4.control import api as control_api, licensing
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import J
from aiflow.v4.core.controlrepo import ControlRepo, Project, epic_for_ticket


SESSION_SECRET = "projects-console-session-secret-with-32-bytes"
OWNER_COOKIE = "aiflow4_session"
SITE = "https://jooservices.atlassian.net"


def owner_sign_in(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set(OWNER_COOKIE, issue_session("owner@example.test", role, 2_000_000_000, SESSION_SECRET))


def prepare_project_control(ctx, control_repo: Path):
    """Make repository refresh read the latest fake bot commit, as RepoCache does after git pull."""
    (control_repo / "versions.lock").write_text(
        "schema: 1\ntools:\n  claude_code: 2.1.305\n  python3: '3.12'\n  uv: '0.8'\n",
        encoding="utf-8")
    ctx.settings.control_repo = str(control_repo)
    ctx.repo = lambda: ControlRepo.load(control_repo)

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


def project_body(project="NEWAPP", **overrides):
    body = {"site": SITE, "project": project, "repo": "jooservices/new-app", "base": "develop",
            "stack": "", "checks": ["vendor/bin/phpunit"], "priority": 3,
            "parallel_limit": 2, "merge_method": "squash"}
    return {**body, **overrides}


def worker_headers(client, tools=None):
    joined = join_node(client, "node1")
    assert joined.status_code == 200, joined.text
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={
        "tools": tools or {"git": "git version 2.43", "uv": "uv 0.8.1", "python3": "Python 3.12.2",
                           "codex": "codex 1.0", "codex-code-mode-host": "present"},
        "slots_total": 2,
    }, headers=headers)
    assert heartbeat.status_code == 200, heartbeat.text
    return headers


def save_project(client, ctx, control_repo, body=None):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    response = client.post("/v1/projects", json=body or project_body())
    assert response.status_code == 200, response.text
    return response


def test_owner_adds_project_from_jira_and_github_catalog_and_worker_claims_ready_ticket(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    headers = worker_headers(client)

    catalog = client.get("/v1/projects/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["jira_sites"] == [SITE]
    assert {p["key"] for p in catalog.json()["jira_projects"]} >= {"NEWAPP"}
    assert "jooservices/new-app" in {r["full_name"] for r in catalog.json()["github_repos"]}

    body = project_body("https://jooservices.atlassian.net/jira/software/c/projects/NEWAPP", stack="python-3.12")
    preflight = client.post("/v1/projects/preflight", json=body)
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["ok"] is True
    node = next(item for item in preflight.json()["nodes"] if item["name"] == "node1")
    assert node["ready"] is True and node["missing_tools"] == []

    saved = client.post("/v1/projects", json=body)
    assert saved.status_code == 200, saved.text
    config_path = control_repo / "projects" / "NEWAPP.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["repo"] == "jooservices/new-app" and config["base"] == "develop"
    assert config["stack"] == "python-3.12" and config["checks"] == ["vendor/bin/phpunit"]
    assert config["priority"] == 3 and config["limits"]["parallel"] == 2
    assert config["merge"]["method"] == "squash" and config["nodes"] == ["cc", "node1"]
    assert saved.json()["commit"].startswith("commit")

    jql: list[str] = []
    ctx.jira.search = lambda query, *args, **kwargs: (jql.append(query) or iter([]))
    from aiflow.v4.control.loops import jira_sync
    jira_sync(ctx, full=True)
    assert jql and "NEWAPP" in jql[-1]

    # The next heartbeat advertises the project and its stack to the worker node.
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={
        "tools": {"git": "git version 2.43", "uv": "uv 0.8.1", "python3": "Python 3.12.2",
                  "codex": "codex 1.0", "codex-code-mode-host": "present"}, "slots_total": 2,
    }, headers=headers)
    assert "NEWAPP" in heartbeat.json()["desired"]["projects"]
    assert heartbeat.json()["desired"]["project_stacks"]["NEWAPP"] == "python-3.12"

    add_ticket(ctx.db, "NEWAPP-1")
    candidates = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=headers)
    assert candidates.status_code == 200, candidates.text
    assert [item["key"] for item in candidates.json()["tickets"]] == ["NEWAPP-1"]
    claim = client.post("/v1/work/claim", json={"ticket": "NEWAPP-1", "account": "codex-01"}, headers=headers)
    assert claim.status_code == 200 and claim.json()["claimed"] is True

    listing = client.get("/v1/projects").json()["projects"]
    project = next(item for item in listing if item["key"] == "NEWAPP")
    assert project["repo"] == "jooservices/new-app" and project["status"] == "active"
    assert project["priority"] == 3 and project["ticket_count"] == 1
    history = ctx.db.all("SELECT actor,action,ticket,accepted,result FROM write_audit"
                         " WHERE target='aiflow-control' AND ticket='NEWAPP' ORDER BY id")
    assert history[-1]["actor"] == "owner@example.test" and history[-1]["action"] == "add_project"
    assert history[-1]["accepted"] is True and "commit" in history[-1]["result"]


def test_concurrent_project_creates_serialize_the_final_licence_slot(ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"]["projects"] = 2
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))
    assert licensing.snapshot(ctx.db, "")["limits"]["projects"] == 2
    assert len(ctx.repo().projects) == 1
    first_client = TestClient(control_api.create_app(ctx))
    second_client = TestClient(control_api.create_app(ctx))
    owner_sign_in(first_client, ctx)
    owner_sign_in(second_client, ctx)
    barrier = threading.Barrier(2)

    def add_project(client_to_use, project):
        barrier.wait()
        return client_to_use.post("/v1/projects", json=project_body(project))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda item: add_project(*item),
                                  ((first_client, "FIRSTAPP"), (second_client, "SECONDAPP"))))
    first_client.close()
    second_client.close()

    assert sorted(response.status_code for response in responses) == [200, 409], (
        [response.text for response in responses], len(ctx.github.commits), len(ctx.repo().projects),
        licensing.snapshot(ctx.db, "")["limits"]["projects"])
    refused = next(response for response in responses if response.status_code == 409)
    assert "Projects limit of 2" in refused.json()["detail"]
    assert "raise the limit in the Licence screen" in refused.json()["detail"]
    assert len(ctx.github.commits) == 1
    assert len(ControlRepo.load(control_repo).projects) == 2


def test_missing_jira_statuses_are_listed_exactly_and_block_every_write(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    ctx.jira.project_workflows["NEWAPP"] = ["Backlog", "To Do", "In Progress", "In Verify", "Done"]
    body = project_body()

    checked = client.post("/v1/projects/preflight", json=body)
    assert checked.status_code == 200
    assert checked.json()["missing_statuses"] == ["Intake", "Needs owner"]
    saved = client.post("/v1/projects", json=body)
    assert saved.status_code == 422
    assert saved.json()["detail"]["missing_statuses"] == ["Intake", "Needs owner"]
    assert ctx.github.commits == []
    assert not (control_repo / "projects" / "NEWAPP.yaml").exists()
    assert ctx.jira.transitions == [] and ctx.jira.fields == [] and ctx.jira.deleted == []
    refusal = ctx.db.one("SELECT accepted,result FROM write_audit WHERE target='aiflow-control'"
                         " AND action='add_project' AND ticket='NEWAPP' ORDER BY id DESC LIMIT 1")
    assert refusal["accepted"] is False and "Intake, Needs owner" in refusal["result"]


def test_missing_base_branch_blocks_save(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    ctx.github.available_branches[("new-app", "develop")] = False
    body = project_body()

    checked = client.post("/v1/projects/preflight", json=body)
    assert checked.status_code == 200 and checked.json()["branch_exists"] is False
    saved = client.post("/v1/projects", json=body)
    assert saved.status_code == 422
    assert "base branch develop is missing" in saved.json()["detail"]["message"]
    assert ctx.github.commits == []
    assert not (control_repo / "projects" / "NEWAPP.yaml").exists()


def test_owner_can_declare_container_checks_from_the_projects_ui_path(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    tools = {"git": "git version 2.43", "uv": "uv 0.8.1", "claude": "Claude 1.0",
             "safe-container-runtime": "rootless Podman 5.4; podman-compose 1.2; project quota"}
    worker_headers(client, tools)
    body = project_body(containers=True, checks=["docker compose run --rm composer composer ci"])
    checked = client.post("/v1/projects/preflight", json=body)
    assert checked.status_code == 200, checked.text
    assert "safe-container-runtime" in checked.json()["required_tools"]
    assert next(item for item in checked.json()["nodes"] if item["name"] == "node1")["ready"] is True

    saved = client.post("/v1/projects", json=body)
    assert saved.status_code == 200, saved.text
    config = yaml.safe_load((control_repo / "projects" / "NEWAPP.yaml").read_text(encoding="utf-8"))
    assert config["containers"] is True


def test_pause_keeps_running_run_alive_and_removes_project_from_candidates_and_sync(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    headers = worker_headers(client)
    save_project(client, ctx, control_repo)
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={"tools": {"git": "2.43", "uv": "0.8",
                                                                           "codex": "1.0",
                                                                           "codex-code-mode-host": "present"}},
                            headers=headers)
    assert "NEWAPP" in heartbeat.json()["desired"]["projects"]
    add_ticket(ctx.db, "NEWAPP-2")
    claim = client.post("/v1/work/claim", json={"ticket": "NEWAPP-2", "account": "codex-01"}, headers=headers)
    assert claim.status_code == 200 and claim.json()["claimed"]
    run_id, fence = claim.json()["run_id"], claim.json()["fence"]

    paused = client.post("/v1/projects/NEWAPP/pause", json={"paused": True})
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    assert yaml.safe_load((control_repo / "projects/NEWAPP.yaml").read_text())["paused"] is True
    assert client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=headers).json()["tickets"] == []
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (run_id,))["status"] == "running"

    completed = client.post(f"/v1/runs/{run_id}/result", json={"fence": fence, "outcome": "done", "summary": "done"},
                            headers=headers)
    assert completed.status_code == 200 and completed.json()["ok"]
    client.post(f"/v1/runs/{run_id}/release", json={"fence": fence}, headers=headers)
    assert ctx.db.one("SELECT status,outcome FROM runs WHERE id=%s", (run_id,)) == {
        "status": "finished", "outcome": "done"}

    jql: list[str] = []
    ctx.jira.search = lambda query, *args, **kwargs: (jql.append(query) or iter([]))
    from aiflow.v4.control.loops import jira_sync
    jira_sync(ctx, full=True)
    assert jql and "NEWAPP" not in jql[-1]


def test_remove_commits_config_removal_but_leaves_jira_and_source_repository_alone(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    headers = worker_headers(client)
    save_project(client, ctx, control_repo)
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={"tools": {"git": "2.43", "uv": "0.8",
                                                                           "codex": "1.0",
                                                                           "codex-code-mode-host": "present"}},
                            headers=headers)
    assert "NEWAPP" in heartbeat.json()["desired"]["projects"]
    add_ticket(ctx.db, "NEWAPP-3")
    assert [t["key"] for t in client.get("/v1/work/candidates", params={"account": "codex-01"},
                                         headers=headers).json()["tickets"]] == ["NEWAPP-3"]
    removed = client.delete("/v1/projects/NEWAPP")
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert not (control_repo / "projects/NEWAPP.yaml").exists()
    assert "NEWAPP" not in {p["key"] for p in client.get("/v1/projects").json()["projects"]}
    assert client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=headers).json()["tickets"] == []
    assert ctx.github.deleted_files == [("aiflow-control", "projects/NEWAPP.yaml")]
    assert ctx.github.repository_deletions == []
    assert ctx.jira.deleted == [] and ctx.jira.transitions == [] and ctx.jira.fields == []
    jql: list[str] = []
    ctx.jira.search = lambda query, *args, **kwargs: (jql.append(query) or iter([]))
    from aiflow.v4.control.loops import jira_sync
    jira_sync(ctx, full=True)
    assert jql and "NEWAPP" not in jql[-1]
    actions = ctx.db.all("SELECT actor,action,ticket,accepted FROM write_audit"
                         " WHERE target='aiflow-control' AND ticket='NEWAPP' ORDER BY id")
    assert [(row["action"], row["accepted"]) for row in actions] == [
        ("add_project", True), ("remove_project", True)]
    assert all(row["actor"] == "owner@example.test" for row in actions)


def test_project_writes_require_owner_session_and_reject_without_changes(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    ctx.settings.console_session_secret = SESSION_SECRET
    response = client.post("/v1/projects", json=project_body())
    assert response.status_code == 401
    assert ctx.github.commits == [] and not (control_repo / "projects/NEWAPP.yaml").exists()
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE target='aiflow-control'"
                      " AND action='add_project' AND ticket='NEWAPP' AND accepted=false")["n"] == 1
    owner_sign_in(client, ctx, role="lead")
    response = client.post("/v1/projects", json=project_body())
    assert response.status_code == 403
    assert ctx.github.commits == [] and not (control_repo / "projects/NEWAPP.yaml").exists()


def test_super_admin_can_resume_and_pause_a_project(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    save_project(client, ctx, control_repo)
    owner_sign_in(client, ctx, role="super_admin")

    paused = client.post("/v1/projects/NEWAPP/pause", json={"paused": True})
    resumed = client.post("/v1/projects/NEWAPP/pause", json={"paused": False})
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    assert resumed.status_code == 200 and resumed.json()["status"] == "active"


def test_project_epic_repository_lookup_inherits_project_defaults_and_all_repo_stacks():
    project = Project(key="MULTI", repo="jooservices/default", base="develop", stack="php-8.3",
                      checks=["composer install"], repositories=[
                          {"repo": "jooservices/core", "base": "main", "stack": "python-3.12",
                           "checks": ["pytest"]}],
                      epic_repositories={"MULTI-2": "jooservices/core"})
    assert project.repo_for({"key": "MULTI-3", "epic": "MULTI-2"}) == "jooservices/core"
    assert project.settings_for_ticket({"epic": "MULTI-2"}) == {
        "repo": "jooservices/core", "base": "main", "checks": ["pytest"], "stack": "python-3.12",
        "containers": False}
    assert project.settings_for_ticket({"key": "MULTI-4"}) == {
        "repo": "jooservices/default", "base": "develop", "checks": ["composer install"], "stack": "php-8.3",
        "containers": False}
    assert set(project.stacks) == {"php-8.3", "python-3.12"}
    assert {"php", "composer", "wp", "python3"}.issubset(project.tools)
    assert epic_for_ticket({"key": "MULTI-4", "type": "Sub-task", "parent_key": "MULTI-3"}, {
        "MULTI-3": {"key": "MULTI-3", "type": "Story", "parent_key": "MULTI-2"},
        "MULTI-2": {"key": "MULTI-2", "type": "Epic", "parent_key": None},
    }) == "MULTI-2"


def test_projects_screen_loads_jira_epics_and_saves_audited_repository_mapping(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    ctx.github.repositories.append({"full_name": "jooservices/core-repo", "name": "core-repo",
                                   "default_branch": "develop"})
    ctx.jira.search_data = [{"key": "TESTWP-12", "fields": {
        "summary": "Core", "issuetype": {"name": "Epic"}, "status": {"name": "In Progress"}}}]

    loaded = client.get("/v1/projects/TESTWP/repositories")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["epics"] == [{"key": "TESTWP-12", "summary": "Core", "status": "In Progress"}]
    assert "jooservices/core-repo" in {r["full_name"] for r in loaded.json()["github_repos"]}
    assert 'issuetype = Epic' in ctx.jira.search_queries[-1]

    saved = client.put("/v1/projects/TESTWP/repositories", json={
        "repositories": [
            {"repo": "jooservices/test-aiflow-wp", "base": "", "checks": [], "stack": ""},
            {"repo": "jooservices/core-repo", "base": "", "checks": [], "stack": ""},
        ],
        "epic_repositories": {"TESTWP-12": "jooservices/core-repo"},
    })
    assert saved.status_code == 200, saved.text
    config = yaml.safe_load((control_repo / "projects/TESTWP.yaml").read_text(encoding="utf-8"))
    assert config["epic_repositories"] == {"TESTWP-12": "jooservices/core-repo"}
    assert {item["repo"] for item in config["repositories"]} == {
        "jooservices/test-aiflow-wp", "jooservices/core-repo"}
    audit_row = ctx.db.one("SELECT actor,action,accepted,result,payload FROM write_audit"
                           " WHERE target='aiflow-control' AND ticket='TESTWP'"
                           " AND action='save_project_repositories' ORDER BY id DESC LIMIT 1")
    assert audit_row["actor"] == "owner@example.test" and audit_row["accepted"] is True
    assert audit_row["payload"]["epic_repositories"] == {"TESTWP-12": "jooservices/core-repo"}
    assert ctx.github.commits[-1]["path"] == "projects/TESTWP.yaml"


def test_dev_claim_uses_parent_epics_repository_and_fenced_github_writes(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    config_path = control_repo / "projects/TESTWP.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["repositories"] = [{"repo": "jooservices/core-repo", "base": "main", "checks": ["pytest"],
                                "stack": "python-3.12"}]
    config["epic_repositories"] = {"TESTWP-50": "jooservices/core-repo"}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    ctx.repo = lambda: ControlRepo.load(control_repo)
    ctx.github.repositories.append({"full_name": "jooservices/core-repo", "name": "core-repo",
                                   "default_branch": "develop"})
    ctx.github.available_branches[("core-repo", "main")] = True
    headers = worker_headers(client)
    ctx.db.run("INSERT INTO tickets (key,project,status,type,labels,summary,description,parent_key)"
               " VALUES ('TESTWP-50','TESTWP','In Progress','Epic',%s,'Core','',NULL)", ([],))
    add_ticket(ctx.db, "TESTWP-51")
    ctx.db.run("UPDATE tickets SET type='Story',parent_key='TESTWP-50' WHERE key='TESTWP-51'")
    add_ticket(ctx.db, "TESTWP-52")
    ctx.db.run("UPDATE tickets SET type='Sub-task',parent_key='TESTWP-51' WHERE key='TESTWP-52'")

    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-52", "account": "codex-01", "step": "dev"},
                         headers=headers)
    assert claimed.status_code == 200, claimed.text
    data = claimed.json()
    assert data["ticket"]["epic"] == "TESTWP-50"
    assert data["project"]["repo"] == "jooservices/core-repo"
    assert data["project"]["base"] == "main" and data["project"]["checks"] == ["pytest"]
    token = client.post("/v1/github/token", json={"run_id": data["run_id"], "fence": data["fence"],
                                                    "repo": "jooservices/core-repo"}, headers=headers)
    assert token.status_code == 200, token.text
    pr = client.post("/v1/github/pr", json={"run_id": data["run_id"], "fence": data["fence"],
                                              "repo": "jooservices/core-repo", "head": "TESTWP-52-work",
                                              "base": "main", "title": "test", "body": "", "draft": True},
                     headers=headers)
    assert pr.status_code == 200, pr.text
    assert ctx.github.token_requests[-1][0] == "core-repo"
    assert ctx.github.prs[-1]["repo"] == "core-repo" and ctx.github.prs[-1]["base"] == "main"


def test_pause_and_remove_write_audits_have_owner_action_project_and_result(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    save_project(client, ctx, control_repo)
    assert client.post("/v1/projects/NEWAPP/pause", json={"paused": True}).status_code == 200
    assert client.post("/v1/projects/NEWAPP/pause", json={"paused": False}).status_code == 200
    assert client.delete("/v1/projects/NEWAPP").status_code == 200
    actions = ctx.db.all("SELECT actor,action,ticket,accepted,result FROM write_audit"
                         " WHERE target='aiflow-control' AND ticket='NEWAPP' ORDER BY id")
    assert [row["action"] for row in actions] == ["add_project", "pause_project", "pause_project", "remove_project"]
    assert all(row["accepted"] and row["ticket"] == "NEWAPP" and row["actor"] == "owner@example.test"
               and row["result"] for row in actions)

