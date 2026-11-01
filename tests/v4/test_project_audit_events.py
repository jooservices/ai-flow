"""Successful project configuration writes publish events for the audit ledger."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aiflow.v4.control import route_deps
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.core.controlrepo import ControlRepo


ACTOR = "owner@example.test"
SESSION_SECRET = "project-audit-events-console-session-secret-over-32-bytes"
SITE = "https://jooservices.atlassian.net"


@pytest.fixture(autouse=True)
def clear_github_connection_context():
    route_deps.github_connection_used.set("")


def owner_sign_in(client, ctx):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session(ACTOR, "owner", 2_000_000_000, SESSION_SECRET))


def prepare_project_control(ctx, control_repo: Path):
    """Make repository refresh read the latest fake bot commit, as RepoCache does after git pull."""
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


def project_body(project="NEWAPP", **overrides):
    body = {"site": SITE, "project": project, "repo": "jooservices/new-app", "base": "develop",
            "stack": "", "checks": ["vendor/bin/phpunit"], "priority": 3,
            "parallel_limit": 2, "merge_method": "squash"}
    return {**body, **overrides}


def assert_project_event_and_audit(db, event_type, *, action, project, actor, result, details,
                                   scope=None):
    event_payload = {"project": project, "actor": actor, "result": result, "details": details}
    if scope is not None:
        event_payload["scope"] = scope
    aggregate_id = scope if scope is not None else project
    events = db.all("SELECT seq,type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s ORDER BY seq",
                    (event_type,))
    assert len(events) == 1
    assert {key: value for key, value in events[0].items() if key != "seq"} == {
        "type": event_type, "aggregate": "project", "aggregate_id": aggregate_id, "payload": event_payload}
    rows = db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                  "WHERE target='aiflow-control' AND action=%s", (action,))
    # route_deps.with_connection adds the GitHub connection a request used; it is not part of the event payload.
    assert len(rows) == 1 and rows[0]["payload"].pop("connection", "default") == "default"
    assert rows == [{"target": "aiflow-control", "action": action, "ticket": project, "run_id": "", "fence": None,
                     "actor": actor, "accepted": True, "result": result, "payload": details or {"project": project}}]


def test_project_repository_save_publishes_event_and_same_audit(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)

    response = client.put("/v1/projects/TESTWP/repositories", json={"repositories": [], "epic_repositories": {}})

    assert response.status_code == 200, response.text
    saved = response.json()
    result = saved["commit"] or "bot commit created"
    details = {"epic_repositories": {}, "repositories": saved["repositories"]}
    assert_project_event_and_audit(ctx.db, "project.repositories_saved", action="save_project_repositories",
                                   project="TESTWP", actor=ACTOR, result=result, details=details)


def test_project_environment_save_publishes_event_and_same_audit(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)

    response = client.put("/v1/projects/TESTWP/environment", json={
        "items": [{"name": "APP_BASE_URL", "kind": "base_url", "value": "https://app.example.test"}],
    })

    assert response.status_code == 200, response.text
    result = response.json()["commit"] or "bot commit created"
    details = {"project": "TESTWP", "names": ["APP_BASE_URL"]}
    assert_project_event_and_audit(ctx.db, "project.environment_saved", action="save_project_environment",
                                   project="TESTWP", actor=ACTOR, result=result, details=details)


def test_environment_scan_publishes_event_and_same_audit(client, ctx):
    owner_sign_in(client, ctx)

    response = client.post("/v1/environment/scan")

    assert response.status_code == 200, response.text
    report = response.json()["scan"]
    result = f"{len(report['offenders'])} of {report['scanned']} open tickets"
    assert_project_event_and_audit(ctx.db, "project.environment_scanned", action="environment_scan",
                                   project="", actor=ACTOR, result=result,
                                   details={"scan": report["id"]}, scope="control")


def test_project_add_publishes_event_and_same_audit(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)

    response = client.post("/v1/projects", json=project_body())

    assert response.status_code == 200, response.text
    result = response.json()["commit"] or "bot commit created"
    details = yaml.safe_load((control_repo / "projects" / "NEWAPP.yaml").read_text(encoding="utf-8"))
    assert_project_event_and_audit(ctx.db, "project.added", action="add_project", project="NEWAPP",
                                   actor=ACTOR, result=result, details=details)


def test_project_services_change_publishes_event_and_same_audit(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)

    response = client.post("/v1/projects/TESTWP/services", json={"services": ["postgres", "redis"]})

    assert response.status_code == 200, response.text
    saved = response.json()
    result = saved["commit"] or "bot commit created"
    assert_project_event_and_audit(ctx.db, "project.services_changed", action="project_services",
                                   project="TESTWP", actor=ACTOR, result=result,
                                   details={"services": saved["services"]})


def test_project_pause_publishes_event_and_same_audit(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)

    response = client.post("/v1/projects/TESTWP/pause", json={"paused": True})

    assert response.status_code == 200, response.text
    result = response.json()["commit"] or "paused by bot commit"
    assert_project_event_and_audit(ctx.db, "project.pause_changed", action="pause_project", project="TESTWP",
                                   actor=ACTOR, result=result, details={"project": "TESTWP", "paused": True})


def test_project_remove_publishes_event_and_same_audit(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)

    response = client.delete("/v1/projects/TESTWP")

    assert response.status_code == 200, response.text
    result = response.json()["commit"] or "bot commit created"
    assert_project_event_and_audit(ctx.db, "project.removed", action="remove_project", project="TESTWP",
                                   actor=ACTOR, result=result, details={"project": "TESTWP"})
