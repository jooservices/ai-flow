"""Knowledge screen CRUD, version history and owner-gated policy rollout."""

from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.control.console_auth import issue_session


SESSION_SECRET = "test-knowledge-console-secret-at-least-32-bytes"


def sign_in(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("knowledge@example.test", role, 2_000_000_000,
                                                         SESSION_SECRET))


def save(client, *, kind, resource_id, title, content, actor="Mai Nguyen", archived=False, version=None):
    return client.post("/v1/knowledge/resources", json={
        "type": kind, "id": resource_id, "title": title, "content": content, "actor": actor,
        "archived": archived, "version": version,
    })


def test_resource_types_commit_to_control_repo_archive_and_restore_with_history(client, ctx):
    sign_in(client, ctx)
    examples = [
        ("study_case", "checkout-migration", "Checkout migration", "Lesson learned: keep the old route until cutover."),
        ("knowledge", "postgres-indexes", "Postgres indexes", "Use a partial index for active records."),
        ("skill", "release-checklist", "Release checklist", "Check signatures before rollout."),
        ("agent", "triage-helper", "Triage helper", "Ask for the failing command and log excerpt."),
        ("guide", "incident-response", "Incident response", "Record impact, owner, and recovery steps."),
    ]
    for kind, resource_id, title, content in examples:
        response = save(client, kind=kind, resource_id=resource_id, title=title, content=content)
        assert response.status_code == 200, response.text
        row = response.json()
        assert row["version"] == 1 and row["updated_by"] == "Mai Nguyen"
        commit = ctx.github.commits[-1]
        assert commit["repo"] == "aiflow-control" and commit["branch"] == "develop"
        assert commit["path"] == row["path"] and "Mai Nguyen" not in commit["content"]
        label = "description: " if kind == "skill" else "title: "
        assert label + title in commit["content"]

        response = save(client, kind=kind, resource_id=resource_id, title=title, content=content + " Revised.",
                        actor="An Tran", version=1)
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 2
        assert save(client, kind=kind, resource_id=resource_id, title=title, content=content,
                    actor="Stale writer", version=1).status_code == 409

        response = save(client, kind=kind, resource_id=resource_id, title=title, content=content + " Revised.",
                        actor="An Tran", archived=True, version=2)
        assert response.status_code == 200 and response.json()["archived"] is True
        archived_commit = ctx.github.commits[-1]["content"]
        assert "archived: true" in archived_commit

        history = client.get(f"/v1/knowledge/resources/{kind}/{resource_id}/history").json()["history"]
        assert [entry["version"] for entry in history] == [3, 2, 1]
        assert history[0]["actor"] == "An Tran" and history[0]["action"] == "archive"
        assert history[1]["content"] == content + " Revised."
        assert history[2]["content"] == content

        restored = client.post(f"/v1/knowledge/resources/{kind}/{resource_id}/restore",
                               json={"version": 1, "actor": "Mai Nguyen"})
        assert restored.status_code == 200 and restored.json()["version"] == 4
        assert restored.json()["archived"] is False
        assert restored.json()["content"] == content

    listed = client.get("/v1/knowledge/resources?include_archived=true").json()["resources"]
    assert {row["type"] for row in listed} >= {item[0] for item in examples}
    assert "aiflow-control" in {row["updated_by"] for row in listed if row["type"] == "policy"}


def test_policy_waits_for_owner_pr_merge_then_refreshes_active_policy(client, ctx):
    sign_in(client, ctx)
    resources = client.get("/v1/knowledge/resources?include_archived=true").json()["resources"]
    current = next(row for row in resources if row["type"] == "policy" and row["id"] == "default")
    prior_content = current["content"]
    new_content = "version: 1\ncommands:\n  allow: ['git status']\n  deny: ['git push*']\nfiles:\n  write_within: worktree\n"
    proposal = save(client, kind="policy", resource_id="default", title="Default policy",
                    content=new_content, version=current["version"])
    assert proposal.status_code == 200, proposal.text
    data = proposal.json()
    assert data["pending"] is True and data["pr_number"] == 1
    pr = ctx.github.prs[-1]
    assert pr["repo"] == "aiflow-control" and pr["base"] == "develop" and pr["draft"] is True
    branch_commit = ctx.github.commits[-1]
    assert branch_commit["branch"].startswith("aiflow/policy-default-")

    # The active row remains unchanged while the pull request is unmerged.
    before = client.get("/v1/knowledge/resources?include_archived=true").json()
    active = next(row for row in before["resources"] if row["type"] == "policy" and row["id"] == "default")
    assert active["content"] == prior_content and active["version"] == current["version"]
    assert before["proposals"][0]["status"] == "pending"

    proposal_id = before["proposals"][0]["proposal_id"]
    client.cookies.clear()
    assert client.post(f"/v1/knowledge/proposals/{proposal_id}/approve", json={"approved_by": "Owner"}).status_code == 401
    assert client.post(f"/v1/knowledge/proposals/{proposal_id}/approve", json={"approved_by": "Owner"},
                       headers={"Authorization": "Bearer review-secret"}).status_code == 401
    refreshes = []
    ctx.refresh_repo = lambda: refreshes.append(True)
    approved = client.post(f"/v1/knowledge/proposals/{proposal_id}/approve", json={"approved_by": "Owner"},
                          headers={"Authorization": "Bearer owner-secret"})
    assert approved.status_code == 200 and approved.json()["merged"] is True
    assert ctx.github.merged[-1] == ("aiflow-control", 1, "Policy: Default policy", "squash")
    assert refreshes == [True]
    sign_in(client, ctx)
    history = client.get("/v1/knowledge/resources/policy/default/history").json()["history"]
    assert history[0]["content"] == new_content and history[0]["actor"] == "Mai Nguyen"
    assert history[0]["approved_by"] == "Owner"
    after = client.get("/v1/knowledge/resources?include_archived=true").json()
    current_after = next(row for row in after["resources"] if row["type"] == "policy" and row["id"] == "default")
    assert current_after["content"] == new_content and current_after["version"] == current["version"] + 1
    assert after["proposals"] == []


def test_project_scoped_policy_files_are_used_by_controlrepo_and_archived_files_are_inactive(control_repo):
    path = control_repo / "policy" / "projects"
    path.mkdir()
    (path / "TESTWP.yaml").write_text("commands:\n  allow: ['pytest *']\n", encoding="utf-8")
    repo = ControlRepo.load(control_repo)
    assert repo.policy_for("TESTWP")["commands"]["allow"] == ["pytest *"]
    (path / "TESTWP.yaml").write_text("# aiflow4-archived: true\ncommands:\n  allow: ['pytest *']\n", encoding="utf-8")
    repo = ControlRepo.load(control_repo)
    assert "TESTWP" not in repo.policy_projects


def test_knowledge_view_exposes_all_resource_workflows(client):
    from aiflow.v4.control.ui import mount_ui
    from fastapi.testclient import TestClient

    app = client.app
    mount_ui(app, app.state.ctx)
    page = TestClient(app).get("/knowledge")
    assert page.status_code == 200
    assert 'data-view="knowledge"' in page.text
    assert "knowledge-history" in page.text and "knowledge-approve" in page.text and "knowledge-restore" in page.text
