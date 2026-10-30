import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import api as control_api
from aiflow.v4.control.db import J
from aiflow.v4.control.loops import jira_sync
from aiflow.v4.control.platform.events import wiring
from aiflow.v4.control import notify
from aiflow.v4.control.review_metrics import calculate_review_metrics
from aiflow.v4.core.controlrepo import Project
from aiflow.v4.core.settings import Settings


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
    ctx.github.repositories.append({"full_name": "jooservices/testflow", "name": "testflow",
                                    "default_branch": "develop"})
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
    assert 'PathPrefix(`/`) && !PathPrefix(`/o2`) && !PathPrefix(`/gatus`)' in compose.read_text()


def test_reviewer_token_is_loaded_from_control_environment(monkeypatch):
    monkeypatch.delenv("AIFLOW4_SECRET_FILES", raising=False)
    monkeypatch.setenv("AIFLOW4_REVIEWER_TOKEN", "configured-review-secret")

    assert Settings.from_env().reviewer_token == "configured-review-secret"


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
    monkeypatch.setattr(control_api.notify, "send", lambda severity, title, detail="", link="", recipient="", **kwargs:
                        calls.append((severity, title, detail, link, recipient)) or ["email"])

    assert jira_sync(ctx) == 1
    assert calls == []  # the notice is a listener of TicketMoved: it runs when the dispatcher delivers the event
    wiring.dispatcher_for(ctx).run_once()
    assert len(calls) == 1
    assert calls[0][0] == "info" and calls[0][1] == "TESTWP-81 is ready for human review"
    assert calls[0][3] == ctx.settings.public_url.rstrip("/") + "/review"
    assert calls[0][4] == "reviewer@example.test"
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-81'")["status"] == "Human review"


def test_worker_transition_notifies_owner_when_assignee_is_missing(client, ctx, monkeypatch):
    add_ticket(ctx.db, "TESTWP-82", labels=("aiflow-ready", "require-human"))
    cc = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {cc['node_token']}"}
    run = client.post("/v1/work/claim", json={"ticket": "TESTWP-82", "account": "claude-01"}, headers=headers).json()
    calls = []
    monkeypatch.setattr(control_api.notify, "send", lambda severity, title, detail="", link="", recipient="", **kwargs:
                        calls.append(recipient) or ["email"])

    response = client.post("/v1/jira/TESTWP-82/transition",
                           json={"run_id": run["run_id"], "fence": run["fence"], "to": "Human review"},
                           headers=headers)
    assert response.json() == {"status": "Human review"}
    wiring.dispatcher_for(ctx).run_once()
    assert calls == [""]  # empty recipient uses the saved channel recipient


def test_notify_email_targets_assignee_and_uses_saved_owner_recipient(ctx, monkeypatch):
    sent = []

    class Mail:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            return None

        def send_message(self, message):
            sent.append((message["To"], message.get("Cc", "")))

    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *_args, **_kwargs: Mail())
    channel = notify.save_channel(ctx.db, {"name": "owner email", "kind": "email", "config": {
        "host": "mail.test", "port": 25, "from": "flow@example.test", "to": "owner@example.test",
        "tls_mode": "none"}})
    notify.save_preferences(ctx.db, {"routes": {"review_before_merge": [channel["id"]]}})
    notify.send("info", "review", recipient="reviewer@example.test", ctx=ctx, alert_kind="review_before_merge")
    notify.send("info", "review", recipient="", ctx=ctx, alert_kind="review_before_merge")

    assert sent == [("owner@example.test", "reviewer@example.test"), ("owner@example.test", "")]


def test_reviewer_approve_merges_with_fence_and_marks_ticket_done(client, ctx):
    add_review_ticket(ctx, "TESTWP-83")
    add_ticket(ctx.db, "TESTWP-84", status="Done")
    response = client.post("/v1/reviews/TESTWP-83/approve", headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200
    assert response.json() == {"merged": True, "sha": "abc123", "status": "Done"}
    assert ctx.github.merged == [("test-aiflow-wp", 42, "TESTWP-83: Summary of TESTWP-83", "squash")]
    assert ctx.jira.status_of["TESTWP-83"] == "Done"
    assert any(key == "TESTWP-83" and "approved" in body for key, body in ctx.jira.comments)
    assert any(number == 42 and "approved" in body for number, body in ctx.github.comments)
    assert ctx.db.one("SELECT step, outcome FROM runs WHERE ticket_key='TESTWP-83' AND account_id='reviewer-console'") == {
        "step": "qa", "outcome": "merged"}
    captured = ctx.db.all("SELECT source_ref FROM knowledge_items WHERE source_type='ticket' ORDER BY source_ref")
    assert [item["source_ref"] for item in captured] == ["TESTWP-83"]
    audit = ctx.db.one("SELECT target, action, accepted, fence FROM write_audit WHERE ticket='TESTWP-83'"
                       " AND action='merge' ORDER BY id DESC LIMIT 1")
    assert audit == {"target": "github", "action": "merge", "accepted": True, "fence": 1}
    # a review decision is the product working, never an operator intervention (JAIF-260 AC6)
    assert ctx.db.one("SELECT count(*) AS n FROM operator_interventions")["n"] == 0


def test_refused_approval_is_recorded_on_jira_pr_and_audit(client, ctx, monkeypatch):
    key = "TESTWP-101"
    add_review_ticket(ctx, key)
    monkeypatch.setattr(ctx.github, "merge_pr", lambda *_args, **_kwargs:
                        {"merged": False, "message": "required checks are pending"})

    response = client.post(f"/v1/reviews/{key}/approve", headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200 and response.json()["merged"] is False
    assert any(ticket == key and "checks are pending" in body for ticket, body in ctx.jira.comments)
    assert any(number == 42 and "checks are pending" in body for number, body in ctx.github.comments)
    assert ctx.db.one("SELECT accepted FROM write_audit WHERE ticket=%s AND target='human-review'",
                      (key,))["accepted"] is False


def test_approval_and_skip_gate_merge_through_the_merge_queue(client, ctx, monkeypatch):
    from aiflow.v4.control import merge_queue
    monkeypatch.setattr(control_api, "HUMAN_QUEUE_WAIT_S", 0)
    add_review_ticket(ctx, "TESTWP-110")
    add_review_ticket(ctx, "TESTWP-111")
    # an earlier ready pull request of the same repository is first in the queue: the approval waits behind it
    repository = ctx.repo().projects["TESTWP"].repo
    merge_queue.turn(ctx.db, repository, 7, "TESTWP-7")
    response = client.post("/v1/reviews/TESTWP-110/approve", headers={"Authorization": "Bearer review-secret"})
    assert response.status_code == 200 and response.json()["merged"] is False
    assert "merge queue" in response.json()["message"] and not getattr(ctx.github, "merged", [])
    assert [e["number"] for e in merge_queue.snapshot(ctx.db, repository)] == [7]  # the approval gave up its place
    merge_queue.finish(ctx.db, repository, 7, True)
    approved = client.post("/v1/reviews/TESTWP-111/skip-gate", headers={"Authorization": "Bearer review-secret"})
    assert approved.status_code == 200 and approved.json()["merged"] is True
    assert len(ctx.github.merged) == 1
    assert ctx.db.one("SELECT state FROM merge_queue WHERE repo=%s AND number=42", (repository,))["state"] == "merged"


def test_reviewer_rework_records_comment_label_and_returns_ticket_to_todo(client, ctx):
    add_review_ticket(ctx, "TESTWP-84")
    response = client.post("/v1/reviews/TESTWP-84/rework", json={"comment": "Please add coverage for retries."},
                           headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200 and response.json()["status"] == "To Do"
    assert ("TESTWP-84", "Human review requested rework:\n\nPlease add coverage for retries.") in ctx.jira.comments
    ticket = ctx.db.one("SELECT status, labels FROM tickets WHERE key='TESTWP-84'")
    assert ticket["status"] == "To Do" and "rework" in ticket["labels"]
    assert any(number == 42 and "requested rework" in body for number, body in ctx.github.comments)
    assert ctx.db.one("SELECT action,accepted FROM write_audit WHERE ticket='TESTWP-84' AND target='human-review'") == {
        "action": "rework", "accepted": True}


def test_reviewer_rework_comment_reaches_the_next_dev_claim(client, ctx):
    key = "TESTWP-87"
    add_review_ticket(ctx, key, labels=("aiflow-ready", "require-human"))
    review = client.post(f"/v1/reviews/{key}/rework", json={"comment": "Please cover the retry timeout."},
                         headers={"Authorization": "Bearer review-secret"})
    assert review.status_code == 200

    joined = join_node(client, "cc").json()
    node = {"Authorization": f"Bearer {joined['node_token']}"}
    dev = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01"}, headers=node).json()

    assert dev["claimed"] and dev["findings"] == "Please cover the retry timeout."


def test_node_merge_endpoint_rejects_qa_runs(client, ctx):
    key = "TESTWP-88"
    add_review_ticket(ctx, key)
    joined = join_node(client, "node1").json()
    node = {"Authorization": f"Bearer {joined['node_token']}"}
    run_id = str(uuid.uuid4())
    claim = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
    assert claim.ok
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,token_hash)"
               " VALUES (%s,%s,'TESTWP','qa','reviewer-console','human','node1',%s,'')",
               (run_id, key, claim.fence))

    response = client.post("/v1/github/merge", json={"run_id": run_id, "fence": claim.fence, "number": 42},
                           headers=node)

    assert response.status_code == 403
    assert getattr(ctx.github, "merged", []) == []


@pytest.mark.parametrize(("action", "body"), [
    ("approve", None),
    ("rework", {"comment": "Please add coverage for retries."}),
])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-secret"])
def test_reviewer_actions_reject_unauthorized_requests_before_claiming_run(client, ctx, action, body, authorization):
    key = "TESTWP-85"
    add_review_ticket(ctx, key)
    headers = {"Authorization": authorization} if authorization else {}

    response = client.post(f"/v1/reviews/{key}/{action}", json=body, headers=headers)

    assert response.status_code == 401
    qa_runs = ctx.db.one("SELECT count(*) AS n FROM runs WHERE ticket_key=%s AND account_id='reviewer-console'", (key,))
    assert qa_runs["n"] == 0
    assert ctx.leases.holder(key) is None
    assert getattr(ctx.github, "merged", []) == []
    assert ctx.jira.comments == []


@pytest.mark.parametrize(("action", "body"), [
    ("approve", None),
    ("rework", {"comment": "Please add coverage for retries."}),
])
def test_reviewer_actions_fail_closed_when_reviewer_token_is_unconfigured(client, ctx, action, body):
    key = "TESTWP-86"
    add_review_ticket(ctx, key)
    ctx.settings.reviewer_token = ""

    response = client.post(f"/v1/reviews/{key}/{action}", json=body,
                           headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 503
    qa_runs = ctx.db.one("SELECT count(*) AS n FROM runs WHERE ticket_key=%s AND account_id='reviewer-console'", (key,))
    assert qa_runs["n"] == 0
    assert ctx.leases.holder(key) is None


def test_reviewer_reject_requires_reason_and_closes_pr_with_audited_jira_and_github_writes(client, ctx):
    key = "TESTWP-89"
    add_review_ticket(ctx, key)
    headers = {"Authorization": "Bearer review-secret"}

    missing = client.post(f"/v1/reviews/{key}/reject", json={"reason": " "}, headers=headers)
    assert missing.status_code == 400 and "reason" in missing.json()["detail"]
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE ticket_key=%s AND account_id='reviewer-console'",
                      (key,))["n"] == 0

    response = client.post(f"/v1/reviews/{key}/reject", json={"reason": "The change exceeds the approved scope."},
                           headers=headers)

    assert response.status_code == 200 and response.json() == {"status": "Done", "closed": True}
    assert ctx.github.pr_by_number[42]["state"] == "closed"
    assert any(ticket == key and "exceeds the approved scope" in body for ticket, body in ctx.jira.comments)
    assert any(number == 42 and "exceeds the approved scope" in body for number, body in ctx.github.comments)
    assert "rejected" in ctx.db.one("SELECT labels FROM tickets WHERE key=%s", (key,))["labels"]
    assert ctx.db.one("SELECT target,action,accepted FROM write_audit WHERE ticket=%s AND target='human-review'",
                      (key,)) == {"target": "human-review", "action": "reject", "accepted": True}
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE ticket=%s AND target IN ('jira','github')"
                      " AND action IN ('comment','close','transition','fields') AND accepted=true", (key,))["n"] >= 5


def test_reviewer_ask_ai_moves_item_to_triage_and_exposes_question_thread(client, ctx):
    key = "TESTWP-90"
    add_review_ticket(ctx, key)
    response = client.post(f"/v1/reviews/{key}/ask-ai", json={"question": "Does this change preserve retries?"},
                           headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200 and response.json()["status"] == "Needs owner"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))["status"] == "Needs owner"
    assert any(key == ticket and "preserve retries" in body for ticket, body in ctx.jira.comments)
    assert any(number == 42 and "preserve retries" in body for number, body in ctx.github.comments)
    item = next(t for t in client.get("/v1/reviews").json()["tickets"] if t["key"] == key)
    assert item["kind"] == "question_from_ai"
    assert item["thread"][0]["kind"] == "question" and "preserve retries" in item["thread"][0]["text"]
    assert ctx.db.one("SELECT action,accepted FROM write_audit WHERE ticket=%s AND target='human-review'",
                      (key,)) == {"action": "ask-ai", "accepted": True}
    node = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {node['node_token']}"}
    candidates = client.get("/v1/work/candidates?account=claude-01", headers=headers).json()["tickets"]
    assert any(candidate["key"] == key and candidate["step"] == "triage" for candidate in candidates)
    claimed = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01", "step": "triage"},
                          headers=headers).json()
    assert claimed["claimed"] and claimed["last_dev_summary"] == "Reviewer question: Does this change preserve retries?"
    assert claimed["pr"]["number"] == 42
    ctx.db.run("UPDATE runs SET status='finished',outcome='answered',summary=%s,details=%s,ended_at=now() WHERE id=%s",
               ("Yes, retry handling is preserved.", J({"answer": "Yes, retry handling is preserved."}), claimed["run_id"]))
    ctx.db.run("UPDATE tickets SET status='To Do' WHERE key=%s", (key,))
    saved_thread = next(t for t in client.get("/v1/reviews").json()["threads"] if t["key"] == key)["thread"]
    assert any(line["kind"] == "ai_answer" and "retry handling is preserved" in line["text"]
               for line in saved_thread)


def test_reviewer_answer_clears_owner_escalation_and_writes_to_jira_pr_and_audit(client, ctx):
    key = "TESTWP-91"
    add_ticket(ctx.db, key, status="Needs owner", labels=("owner-escalated",))
    add_review_run(ctx, key)
    response = client.post(f"/v1/reviews/{key}/answer", json={"answer": "Use the shared cache."},
                           headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200 and response.json()["status"] == "To Do"
    ticket = ctx.db.one("SELECT status,labels FROM tickets WHERE key=%s", (key,))
    assert ticket["status"] == "To Do" and "owner-escalated" not in ticket["labels"]
    assert any(key == ticket_key and "Use the shared cache" in body for ticket_key, body in ctx.jira.comments)
    assert any(number == 42 and "Use the shared cache" in body for number, body in ctx.github.comments)
    assert ctx.db.one("SELECT action,accepted FROM write_audit WHERE ticket=%s AND target='human-review'",
                      (key,)) == {"action": "answer", "accepted": True}


def test_reviewer_answer_rejects_blocked_item_before_claiming_run(client, ctx):
    key = "TESTWP-103"
    blocker = "TESTWP-104"
    add_ticket(ctx.db, blocker, status="In Progress")
    add_ticket(ctx.db, key, status="Needs owner", labels=("owner-escalated",), blocked_by=(blocker,))
    add_review_run(ctx, key)

    response = client.post(f"/v1/reviews/{key}/answer", json={"answer": "Use the shared cache."},
                           headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 409
    assert "current human review item" in response.json()["detail"]
    assert ctx.db.one("SELECT status,labels FROM tickets WHERE key=%s", (key,)) == {
        "status": "Needs owner", "labels": ["owner-escalated"]}
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE ticket_key=%s AND account_id='reviewer-console'",
                      (key,))["n"] == 0
    assert ctx.leases.holder(key) is None
    assert ctx.jira.comments == []
    assert getattr(ctx.github, "comments", []) == []


def test_reviewer_skip_gate_merges_and_removes_the_gate_label(client, ctx):
    key = "TESTWP-92"
    add_review_ticket(ctx, key)
    add_ticket(ctx.db, "TESTWP-93", status="Done")
    response = client.post(f"/v1/reviews/{key}/skip-gate", headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200 and response.json()["merged"] is True
    assert ctx.github.merged and ctx.github.merged[-1][1] == 42
    assert "require-human" not in ctx.db.one("SELECT labels FROM tickets WHERE key=%s", (key,))["labels"]
    assert any(ticket == key and "gate skipped" in body for ticket, body in ctx.jira.comments)
    assert any(number == 42 and "gate skipped" in body for number, body in ctx.github.comments)
    assert ctx.db.one("SELECT action,accepted FROM write_audit WHERE ticket=%s AND target='human-review'",
                      (key,)) == {"action": "skip-gate", "accepted": True}
    captured = ctx.db.all("SELECT source_ref FROM knowledge_items WHERE source_type='ticket' ORDER BY source_ref")
    assert [item["source_ref"] for item in captured] == [key]


def test_review_queue_offers_actions_by_waiting_item_kind(client, ctx):
    add_review_ticket(ctx, "TESTWP-96")
    add_ticket(ctx.db, "TESTWP-97", status="Needs owner", labels=("owner-escalated",))
    add_review_run(ctx, "TESTWP-97")
    add_ticket(ctx.db, "TESTWP-98", status="Needs owner")
    add_review_run(ctx, "TESTWP-98", step="dev", pr_url="", outcome="blocked", summary="Which cache? ")
    add_ticket(ctx.db, "TESTWP-99", status="To Do")
    add_ticket(ctx.db, "TESTWP-100", status="In Progress", blocked_by=("TESTWP-99",))

    items = {ticket["key"]: ticket for ticket in client.get("/v1/reviews").json()["tickets"]}

    assert items["TESTWP-96"]["actions"] == ["approve", "reject", "rework", "ask-ai", "skip-gate"]
    assert items["TESTWP-97"]["kind"] == "decision_needed"
    assert items["TESTWP-97"]["actions"] == ["answer", "ask-ai"]
    assert items["TESTWP-98"]["kind"] == "question_from_ai"
    assert items["TESTWP-98"]["actions"] == ["answer", "ask-ai"]
    assert items["TESTWP-100"]["kind"] == "blocked"
    assert items["TESTWP-100"]["actions"] == ["ask-ai"]


def test_review_queue_hides_pull_request_actions_when_no_pr_exists(client, ctx):
    key = "TESTWP-105"
    add_ticket(ctx.db, key, status="Human review", labels=("require-human",))
    add_review_run(ctx, key, pr_url="")

    item = next(ticket for ticket in client.get("/v1/reviews").json()["tickets"] if ticket["key"] == key)

    assert item["pr"]["number"] is None
    assert item["actions"] == ["rework", "ask-ai"]


def test_reviewer_action_required_text_is_validated_before_claiming_a_fenced_run(client, ctx):
    key = "TESTWP-93"
    add_review_ticket(ctx, key)
    headers = {"Authorization": "Bearer review-secret"}
    for action, body in (("rework", {"comment": " "}), ("reject", {"reason": " "}),
                         ("ask-ai", {"question": " "})):
        response = client.post(f"/v1/reviews/{key}/{action}", json=body, headers=headers)
        assert response.status_code == 400
    add_ticket(ctx.db, "TESTWP-94", status="Needs owner")
    add_review_run(ctx, "TESTWP-94")
    response = client.post("/v1/reviews/TESTWP-94/answer", json={"answer": " "}, headers=headers)
    assert response.status_code == 400
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id='reviewer-console'")["n"] == 0


def test_review_metrics_project_filter_and_csv_export(client, ctx):
    add_review_ticket(ctx, "TESTWP-95")
    add_ticket(ctx.db, "OTHER-1", status="Human review", labels=("require-human",))
    add_review_run(ctx, "OTHER-1")
    ctx.repo().projects["OTHER"] = ctx.repo().projects["TESTWP"]

    response = client.get("/v1/reviews?project=TESTWP")
    assert response.status_code == 200
    assert {ticket["key"] for ticket in response.json()["tickets"]} == {"TESTWP-95"}
    assert response.json()["metrics"]["people_needed_this_week"] == 1
    assert client.get("/v1/reviews").json()["metrics"]["people_needed_this_week"] == 2
    assert client.get("/v1/reviews?project=NOTCONFIGURED").status_code == 400
    export = client.get("/v1/reviews/export.csv?project=TESTWP")
    assert export.status_code == 200 and "text/csv" in export.headers["content-type"]
    assert "people_needed_this_week" in export.text and "question_from_ai" in export.text
    assert ",TESTWP" in export.text


def test_review_metric_calculations_and_weekly_reason_split():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    monday = datetime(2026, 10, 19, tzinfo=timezone.utc)
    eight_weeks_before = monday - timedelta(weeks=8)
    tickets = [
        {"key": "T-1", "status": "Done", "labels": [], "blocked_by": [], "done_at": now - timedelta(days=1)},
        {"key": "T-2", "status": "Done", "labels": [], "blocked_by": [], "done_at": now - timedelta(days=1)},
        {"key": "T-3", "status": "In Progress", "labels": [], "blocked_by": [], "done_at": None},
        {"key": "T-4", "status": "Needs owner", "labels": ["owner-escalated"], "blocked_by": [],
         "done_at": None},
        {"key": "T-5", "status": "Human review", "labels": ["require-human"], "blocked_by": [],
         "done_at": None},
        {"key": "T-6", "status": "In Progress", "labels": [], "blocked_by": ["T-99"],
         "jira_updated": now - timedelta(days=1)},
    ]
    events = [
        {"type": "jira.transition", "ticket": "T-1", "outcome": "Human review", "ts": monday + timedelta(minutes=1)},
        {"type": "jira.transition", "ticket": "T-1", "outcome": "Done", "ts": monday + timedelta(minutes=21)},
        {"type": "jira.transition", "ticket": "T-3", "outcome": "Needs owner", "ts": eight_weeks_before},
        {"type": "jira.transition", "ticket": "T-3", "outcome": "To Do", "ts": eight_weeks_before + timedelta(minutes=40)},
        {"type": "jira.transition", "ticket": "T-4", "outcome": "Needs owner", "ts": monday + timedelta(hours=2)},
        {"type": "jira.transition", "ticket": "T-5", "outcome": "Human review", "ts": monday + timedelta(hours=3)},
    ]
    runs = [
        {"ticket_key": "T-3", "step": "dev", "outcome": "blocked", "account_id": "codex-01",
         "started_at": eight_weeks_before - timedelta(hours=1)},
        {"ticket_key": "T-5", "step": "dev", "outcome": "done", "account_id": "codex-01",
         "started_at": monday - timedelta(days=1)},
        {"ticket_key": "T-5", "step": "qa", "outcome": "rework", "account_id": "reviewer-console",
         "started_at": monday},
    ]
    audits = [
        {"ticket": "T-1", "target": "jira", "action": "transition", "accepted": True,
         "ts": monday + timedelta(minutes=1, seconds=1), "payload": {"to": "Human review"}},
        {"ticket": "T-1", "target": "jira", "action": "transition", "accepted": True,
         "ts": monday + timedelta(minutes=21, seconds=1), "payload": {"to": "Done"}},
        {"ticket": "T-1", "target": "human-review", "action": "rework", "accepted": True,
         "ts": monday + timedelta(minutes=1)},
        {"ticket": "T-1", "target": "human-review", "action": "approve", "accepted": True,
         "ts": monday + timedelta(minutes=2)},
        {"ticket": "T-5", "target": "human-review", "action": "approve", "accepted": True,
         "ts": monday + timedelta(minutes=3)},
    ]
    result = calculate_review_metrics(tickets, events, runs, audits, now)

    assert result["people_needed_this_week"] == 4
    assert result["people_needed_eight_weeks_ago"] == 1
    assert result["weekly"][-1]["by_reason"] == {
        "question_from_ai": 0, "decision_needed": 1, "review_before_merge": 2, "blocked": 1}
    assert result["weekly"][0]["by_reason"]["question_from_ai"] == 1
    assert result["finished_with_no_person_pct"] == 50
    assert result["median_wait_minutes"] == 1730
    assert result["approved_first_time_pct"] == 50
    assert result["sent_back_or_rejected_pct"] == 50


def test_review_metrics_use_transition_audits_and_require_human_labels_when_events_are_missing():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    result = calculate_review_metrics(
        [{"key": "T-10", "status": "Done", "labels": ["require-human"], "blocked_by": [],
          "done_at": now - timedelta(days=1)},
         {"key": "T-11", "status": "Done", "labels": ["require-human"], "blocked_by": [],
          "done_at": now - timedelta(days=1)}],
        [], [],
        [{"ticket": "T-10", "target": "jira", "action": "transition", "accepted": True,
          "ts": now - timedelta(days=2), "payload": {"to": "Human review"}}],
        now,
    )

    assert result["people_needed_this_week"] == 1
    assert result["weekly"][-1]["by_reason"]["review_before_merge"] == 1
    assert result["finished_with_no_person_pct"] == 0


def test_review_metrics_do_not_count_ai_handoff_as_a_person_wait():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    asked_at = now - timedelta(minutes=5)
    result = calculate_review_metrics(
        [{"key": "T-12", "status": "To Do", "labels": [], "blocked_by": []}],
        [{"type": "jira.transition", "ticket": "T-12", "outcome": "Needs owner", "reason": "reviewer asked the AI",
          "ts": asked_at},
         {"type": "jira.transition", "ticket": "T-12", "outcome": "To Do", "ts": now - timedelta(minutes=1)}],
        [],
        [{"ticket": "T-12", "target": "jira", "action": "transition", "accepted": True,
          "ts": asked_at + timedelta(seconds=1), "payload": {"to": "Needs owner"}}],
        now,
    )

    assert result["people_needed_this_week"] == 0
    assert result["median_wait_minutes"] is None
