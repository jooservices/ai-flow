import uuid

from conftest import add_ticket
from aiflow.v4.control import api as control_api
from aiflow.v4.control.db import J
from aiflow.v4.control.loops import jira_sync
from aiflow.v4.control import notify
from aiflow.v4.core.controlrepo import Project


def add_review_run(ctx, key, step="dev", pr_url="https://github.com/jooservices/test-aiflow-wp/pull/42",
                   outcome="done", summary="", details=None):
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                "pr_url,summary,details) VALUES (%s,%s,%s,%s,'codex-01','codex','node1',1,'finished',%s,%s,%s,%s)",
                (run_id, key, key.split("-", 1)[0], step, outcome, pr_url, summary, J(details or {})))
    return run_id


def add_review_ticket(ctx, key="TESTWP-80", labels=("require-human",)):
    add_ticket(ctx.db, key, status="Human review", labels=labels)
    add_review_run(ctx, key, summary="Developer summary")
    add_review_run(ctx, key, step="verify", pr_url="", outcome="human", summary="verify passed",
                   details={"verdict": "PASS", "checks": "unit tests passed"})


def test_review_queue_shows_summary_pr_diff_verify_and_gated_preview(client, ctx):
    ctx.repo().projects["TESTFLOW"] = Project(
        key="TESTFLOW", repo="jooservices/testflow", environments={"preview": {"url": "https://preview.testflow.test"}}
    )
    add_ticket(ctx.db, "TESTFLOW-5", status="To Do", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTFLOW-5'")
    add_ticket(ctx.db, "TESTFLOW-28", status="Human review", labels=("require-human",))
    add_review_run(ctx, "TESTFLOW-28", pr_url="https://github.com/jooservices/testflow/pull/42", summary="Implemented queue")
    add_review_run(ctx, "TESTFLOW-28", step="verify", pr_url="", outcome="human", summary="PASS: verified",
                   details={"verdict": "PASS", "checks": "pytest passed"})
    add_ticket(ctx.db, "TESTFLOW-29", status="To Do")
    ctx.github.pr_data = {42: {"title": "TESTFLOW-28: Reviewer queue", "additions": 80,
                               "deletions": 12, "changed_files": 4}}

    queued = client.get("/v1/reviews").json()["tickets"]
    assert [t["key"] for t in queued] == ["TESTFLOW-28"]
    ticket = queued[0]
    assert ticket["summary"] == "Summary of TESTFLOW-28"
    assert ticket["pr"] == {"url": "https://github.com/jooservices/testflow/pull/42", "number": 42,
                            "title": "TESTFLOW-28: Reviewer queue", "additions": 80, "deletions": 12,
                            "changed_files": 4}
    assert ticket["verify"] == {"outcome": "human", "summary": "PASS: verified", "verdict": "PASS",
                                "checks": "pytest passed", "findings": ""}
    assert ticket["preview_url"] == ""

    ctx.db.run("UPDATE tickets SET status='Done' WHERE key='TESTFLOW-5'")
    assert client.get("/v1/reviews").json()["tickets"][0]["preview_url"] == "https://preview.testflow.test"


def test_review_page_is_routed_by_traefik():
    from pathlib import Path

    compose = Path(__file__).resolve().parents[2] / "deploy" / "v4" / "control" / "compose.yaml"
    assert "PathPrefix(`/review`)" in compose.read_text()


def test_jira_sync_notifies_assignee_when_require_human_ticket_enters_review(ctx, monkeypatch):
    add_ticket(ctx.db, "TESTWP-81", status="To Do", labels=("aiflow-ready", "require-human"))
    ctx.db.run("UPDATE tickets SET fields=%s WHERE key='TESTWP-81'",
               (J({"assignee": {"name": "Reviewer", "email": "reviewer@example.test"}}),))
    issue = {"key": "TESTWP-81", "fields": {
        "summary": "Needs human review", "status": {"name": "Human review"},
        "issuetype": {"name": "Task"}, "priority": {"name": "High"},
        "labels": ["aiflow-ready", "require-human"], "description": "Review this change",
        "assignee": {"displayName": "Reviewer", "emailAddress": "reviewer@example.test", "accountId": "abc"},
    }}
    ctx.jira.search = lambda _jql: iter([issue])
    calls = []
    monkeypatch.setattr(control_api.notify, "send", lambda severity, title, detail="", link="", recipient="":
                        calls.append((severity, title, detail, link, recipient)) or ["email"])

    assert jira_sync(ctx) == 1
    assert len(calls) == 1
    assert calls[0][0] == "info" and calls[0][1] == "TESTWP-81 is ready for human review"
    assert calls[0][4] == "reviewer@example.test"
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-81'")["status"] == "Human review"


def test_worker_transition_notifies_owner_when_assignee_is_missing(client, ctx, monkeypatch):
    add_ticket(ctx.db, "TESTWP-82", labels=("aiflow-ready", "require-human"))
    cc = client.post("/v1/nodes/join", json={"name": "cc", "join_token": "join-secret"}).json()
    headers = {"Authorization": f"Bearer {cc['node_token']}"}
    run = client.post("/v1/work/claim", json={"ticket": "TESTWP-82", "account": "claude-01"}, headers=headers).json()
    calls = []
    monkeypatch.setattr(control_api.notify, "send", lambda severity, title, detail="", link="", recipient="":
                        calls.append(recipient) or ["email"])

    response = client.post("/v1/jira/TESTWP-82/transition",
                           json={"run_id": run["run_id"], "fence": run["fence"], "to": "Human review"},
                           headers=headers)
    assert response.json() == {"status": "Human review"}
    assert calls == [""]  # notify.send routes an empty recipient to the configured owner alert email


def test_notify_email_targets_assignee_and_uses_owner_as_fallback(monkeypatch):
    monkeypatch.setenv("AIFLOW4_SMTP_HOST", "mail.test")
    monkeypatch.setenv("AIFLOW4_ALERT_EMAIL", "owner@example.test")
    sent = []

    class Mail:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send_message(self, message):
            sent.append(message["To"])

    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *_args, **_kwargs: Mail())
    notify.send("info", "review", recipient="reviewer@example.test")
    notify.send("info", "review", recipient="")

    assert sent == ["reviewer@example.test", "owner@example.test"]


def test_reviewer_approve_merges_with_fence_and_marks_ticket_done(client, ctx):
    add_review_ticket(ctx, "TESTWP-83")
    response = client.post("/v1/reviews/TESTWP-83/approve")

    assert response.status_code == 200
    assert response.json() == {"merged": True, "sha": "abc123", "status": "Done"}
    assert ctx.github.merged == [("test-aiflow-wp", 42, "TESTWP-83: Summary of TESTWP-83", "squash")]
    assert ctx.jira.status_of["TESTWP-83"] == "Done"
    assert any(key == "TESTWP-83" and "approved" in body for key, body in ctx.jira.comments)
    assert ctx.db.one("SELECT step, outcome FROM runs WHERE ticket_key='TESTWP-83' AND account_id='reviewer-console'") == {
        "step": "qa", "outcome": "merged"}
    audit = ctx.db.one("SELECT target, action, accepted, fence FROM write_audit WHERE ticket='TESTWP-83'"
                       " AND action='merge' ORDER BY id DESC LIMIT 1")
    assert audit == {"target": "github", "action": "merge", "accepted": True, "fence": 1}


def test_reviewer_rework_records_comment_label_and_returns_ticket_to_todo(client, ctx):
    add_review_ticket(ctx, "TESTWP-84")
    response = client.post("/v1/reviews/TESTWP-84/rework", json={"comment": "Please add coverage for retries."})

    assert response.status_code == 200 and response.json()["status"] == "To Do"
    assert ("TESTWP-84", "Human review requested rework:\n\nPlease add coverage for retries.") in ctx.jira.comments
    ticket = ctx.db.one("SELECT status, labels FROM tickets WHERE key='TESTWP-84'")
    assert ticket["status"] == "To Do" and "rework" in ticket["labels"]
