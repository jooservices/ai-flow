"""Stakeholder deployment links, review data, and Jira verdicts."""
from __future__ import annotations

from pathlib import Path
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control.api import create_app
from aiflow.v4.control.db import J
from aiflow.v4.control.jira import Jira
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.control.ui import mount_ui
from conftest import add_ticket


def successful_change(ctx, key="TESTWP-20", *, epic=""):
    add_ticket(ctx.db, key)
    if epic:
        add_ticket(ctx.db, epic)
        ctx.db.run("UPDATE tickets SET type='Epic' WHERE key=%s", (epic,))
        ctx.db.run("UPDATE tickets SET parent_key=%s WHERE key=%s", (epic, key))
    ctx.db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,branch,head_sha,"
        "summary,details,ended_at) VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',1,'finished','done',"
        "'TESTWP-20-feature','abc123','Implemented stakeholder flow',%s,now())",
        (str(uuid.uuid4()), key, J({"release": "v4.2.0"})),
    )


def stakeholder_client(ctx, tmp_path):
    client = TestClient(create_app(ctx))
    mount_ui(client.app, ctx)
    client.app.state.sync_accounts()
    return client


def test_project_urls_load_and_missing_urls_default_to_empty(control_repo):
    project_path = control_repo / "projects" / "TESTWP.yaml"
    with_urls = project_path.read_text().replace("base: develop", "base: develop\nurls:\n  develop: https://uat.example.test\n  production: https://www.example.test")
    project_path.write_text(with_urls)
    loaded = ControlRepo.load(control_repo).projects["TESTWP"]
    assert loaded.urls == {"develop": "https://uat.example.test", "production": "https://www.example.test"}

    project_path.write_text(project_path.read_text().replace(
        "urls:\n  develop: https://uat.example.test\n  production: https://www.example.test\n", ""))
    assert ControlRepo.load(control_repo).projects["TESTWP"].urls == {}


def test_stakeholder_urls_normalize_missing_keys_to_empty(ctx, tmp_path):
    ctx.repo().projects["TESTWP"].urls = {"develop": "https://uat.example.test", "production": ""}
    client = stakeholder_client(ctx, tmp_path)
    page = client.get("/stakeholder")
    assert page.status_code == 200 and 'id="view-stakeholder"' in page.text
    response = client.get("/v1/stakeholder/TESTWP")
    assert response.status_code == 200
    assert response.json()["urls"] == {"develop": "https://uat.example.test", "production": ""}


def test_stakeholder_page_is_routed_by_traefik():
    compose = Path(__file__).resolve().parents[2] / "deploy" / "v4" / "control" / "compose.yaml"
    assert "PathPrefix(`/stakeholder`)" in compose.read_text()


def test_stakeholder_change_feed_shows_latest_ticket_epic_and_release_or_commit(ctx, tmp_path):
    successful_change(ctx, epic="TESTWP-2")
    ctx.db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,head_sha,details,"
        "started_at,ended_at) VALUES (%s,'TESTWP-20','TESTWP','dev','claude-01','claude','cc',1,'finished','done',"
        "'older-sha',%s,now()-interval '1 day',now()-interval '1 day')",
        (str(uuid.uuid4()), J({"release": "older-release"})),
    )
    client = stakeholder_client(ctx, tmp_path)
    changes = client.get("/v1/stakeholder/TESTWP").json()["changes"]
    assert changes[0]["key"] == "TESTWP-20"
    assert changes[0]["epic"] == "TESTWP-2"
    assert changes[0]["release_commit"] == "v4.2.0"
    assert changes[0]["commit"] == "abc123"
    assert changes[0]["change_summary"] == "Implemented stakeholder flow"


@pytest.mark.parametrize("screenshot", [None, "data:image/png;base64,cG5n"])
def test_not_expected_creates_bug_with_required_fields_status_label_and_optional_screenshot(ctx, tmp_path, screenshot):
    ctx.repo().projects["TESTWP"].urls = {"develop": "https://uat.example.test", "production": "https://www.example.test"}
    successful_change(ctx)
    client = stakeholder_client(ctx, tmp_path)
    response = client.post("/v1/stakeholder/TESTWP/verdict", json={
        "change_key": "TESTWP-20", "environment": "production", "verdict": "not_expected",
        "sender": "Mai Nguyen", "release_commit": "v4.2.0 / abc123", "expected": "Trang hiển thị số mới",
        "actual": "Trang vẫn hiện số cũ", "screenshot_data": screenshot or "", "screenshot_filename": "uat.png",
    })
    assert response.status_code == 200
    key = response.json()["key"]
    created = ctx.jira.created[0]
    assert created["project"] == "TESTWP" and created["issuetype"] == "Bug"
    assert created["labels"] == ["uat-bug"] and ctx.jira.status_of[key] == "Intake"
    for value in ("https://www.example.test", "production", "v4.2.0 / abc123", "Trang hiển thị số mới",
                  "Trang vẫn hiện số cũ", "Mai Nguyen", "TESTWP-20"):
        assert value in created["description"]
    mirrored = ctx.db.one("SELECT status,type,labels,description FROM tickets WHERE key=%s", (key,))
    assert (mirrored["status"], mirrored["type"], mirrored["labels"]) == ("Intake", "Bug", ["uat-bug"])
    if screenshot:
        assert ctx.jira.attachments == [(key, "uat.png", b"png", "image/png")]
    else:
        assert ctx.jira.attachments == []


def test_expected_verdict_comments_on_related_epic_without_creating_issue(ctx, tmp_path):
    successful_change(ctx, epic="TESTWP-2")
    client = stakeholder_client(ctx, tmp_path)
    response = client.post("/v1/stakeholder/TESTWP/verdict", json={
        "change_key": "TESTWP-20", "target_key": "TESTWP-2", "environment": "develop", "verdict": "expected",
        "sender": "Mai Nguyen", "release_commit": "abc123",
    })
    assert response.status_code == 200 and response.json()["ticket"] == "TESTWP-2"
    assert ctx.jira.comments == [("TESTWP-2", "Comment nghiệm thu\nMôi trường: develop\n"
                                  "Release hoặc commit: abc123\nNgười gửi: Mai Nguyen")]
    assert not getattr(ctx.jira, "created", [])


def test_jira_failure_is_returned_as_an_error_not_success(ctx, tmp_path):
    successful_change(ctx)
    def fail_comment(key, text):
        raise RuntimeError("jira unavailable")
    ctx.jira.comment = fail_comment
    client = stakeholder_client(ctx, tmp_path)
    response = client.post("/v1/stakeholder/TESTWP/verdict", json={
        "change_key": "TESTWP-20", "environment": "develop", "verdict": "expected", "sender": "Mai Nguyen",
    })
    assert response.status_code == 502
    assert response.json()["detail"] == "Jira could not record the acceptance"


def test_bug_creation_failure_is_not_reported_as_success(ctx, tmp_path):
    successful_change(ctx)
    def fail_create(*args, **kwargs):
        raise RuntimeError("jira unavailable")
    ctx.jira.create_issue = fail_create
    client = stakeholder_client(ctx, tmp_path)
    response = client.post("/v1/stakeholder/TESTWP/verdict", json={
        "change_key": "TESTWP-20", "environment": "develop", "verdict": "not_expected", "sender": "Mai Nguyen",
        "expected": "A", "actual": "B",
    })
    assert response.status_code == 502
    assert response.json()["detail"] == "Jira could not record the UAT feedback"


def test_bad_feedback_and_unrelated_acceptance_target_are_rejected(ctx, tmp_path):
    successful_change(ctx, epic="TESTWP-2")
    add_ticket(ctx.db, "TESTWP-99")
    client = stakeholder_client(ctx, tmp_path)
    common = {"change_key": "TESTWP-20", "environment": "develop", "sender": "Mai Nguyen"}
    missing_content = client.post("/v1/stakeholder/TESTWP/verdict", json={**common, "verdict": "not_expected"})
    assert missing_content.status_code == 400
    unrelated = client.post("/v1/stakeholder/TESTWP/verdict", json={**common, "verdict": "expected", "target_key": "TESTWP-99"})
    assert unrelated.status_code == 400


def test_jira_attachment_uses_multipart_rest_v3_request():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "attachment-1"})
    jira = Jira("https://jira.example.test", "person@example.test", "token",
                client=httpx.Client(transport=httpx.MockTransport(respond), base_url="https://jira.example.test"))
    jira.add_attachment("TESTWP-90", "screenshot.png", b"image-bytes", "image/png")
    request = requests[0]
    assert request.method == "POST" and request.url.path == "/rest/api/3/issue/TESTWP-90/attachments"
    assert request.headers["X-Atlassian-Token"] == "no-check"
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b"screenshot.png" in request.content and b"image-bytes" in request.content

