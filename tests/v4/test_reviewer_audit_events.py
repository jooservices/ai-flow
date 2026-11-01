"""Reviewer and flow-bug writes keep their audit rows through the domain event ledger."""
from __future__ import annotations

import uuid

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import route_deps
from aiflow.v4.control.db import J


AUDIT_COLUMNS = "target,action,ticket,run_id,fence,actor,accepted,result,payload"
MASTER_SCOPE = "master:global"


def assert_event_and_audit(db, event_type, *, aggregate_id, event_payload, target, action, ticket, run_id, fence,
                           actor, accepted=True, result, payload):
    events = db.all("SELECT seq,type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s ORDER BY seq",
                    (event_type,))
    assert len(events) == 1
    assert {key: value for key, value in events[0].items() if key != "seq"} == {
        "type": event_type, "aggregate": "run", "aggregate_id": aggregate_id, "payload": event_payload}
    rows = db.all(f"SELECT {AUDIT_COLUMNS} FROM write_audit WHERE target=%s AND action=%s AND ticket=%s"
                  " AND accepted=%s ORDER BY id", (target, action, ticket, accepted))
    # route_deps.with_connection adds the GitHub connection a request used; it is not part of the event payload.
    for row in rows:
        assert row["payload"].pop("connection", "default") == "default"
    assert rows == [{"target": target, "action": action, "ticket": ticket, "run_id": run_id, "fence": fence,
                     "actor": actor, "accepted": accepted, "result": result, "payload": payload}]


def assert_event_absent(db, event_type, *, ticket):
    events = db.all("SELECT seq,type FROM domain_events WHERE type=%s AND payload->>'ticket'=%s ORDER BY seq",
                    (event_type, ticket))
    assert events == []


def add_review_run(ctx, key, step="dev", pr_url="https://github.com/jooservices/test-aiflow-wp/pull/42",
                   outcome="done", summary="", details=None):
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                "pr_url,summary,details) VALUES (%s,%s,%s,%s,'codex-01','codex','node1',1,'finished',%s,%s,%s,%s)",
                (run_id, key, key.split("-", 1)[0], step, outcome, pr_url, summary, J(details or {})))
    return run_id


def add_review_ticket(ctx, key, *, status="Human review", labels=("require-human",)):
    add_ticket(ctx.db, key, status=status, labels=labels)
    add_review_run(ctx, key, summary="Developer summary")
    if status == "Human review":
        add_review_run(ctx, key, step="verify", pr_url="", outcome="human", summary="verify passed",
                       details={"verdict": "PASS", "checks": "unit tests passed"})


def test_rework_publishes_reviewer_comment_transition_labels_and_action_audits(client, ctx):
    key = "TESTWP-4781"
    add_review_ticket(ctx, key)
    route_deps.github_connection_used.set("")

    response = client.post(f"/v1/reviews/{key}/rework", json={"comment": "Please add coverage for retries."},
                           headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200, response.text
    run = ctx.db.one("SELECT id,fence,account_id FROM runs WHERE ticket_key=%s AND account_id='reviewer-console'",
                     (key,))
    run_id, fence, actor = str(run["id"]), run["fence"], run["account_id"]
    note = "Human review requested rework:\n\nPlease add coverage for retries."
    assert_event_and_audit(ctx.db, "review.jira_comment_posted", aggregate_id=run_id,
                           event_payload={"run_id": run_id, "fence": fence, "ticket": key, "actor": actor,
                                          "comment_id": "1", "source": "human-review", "chars": len(note)},
                           target="jira", action="comment", ticket=key, run_id=run_id, fence=fence,
                           actor=actor, result="1", payload={"source": "human-review", "chars": len(note)})
    assert_event_and_audit(ctx.db, "review.github_comment_posted", aggregate_id=run_id,
                           event_payload={"run_id": run_id, "fence": fence, "ticket": key, "actor": actor,
                                          "source": "human-review", "number": 42,
                                          "repo": "jooservices/test-aiflow-wp"},
                           target="github", action="comment", ticket=key, run_id=run_id, fence=fence,
                           actor=actor, result="ok", payload={"source": "human-review", "number": 42,
                                                               "repo": "jooservices/test-aiflow-wp"})
    assert_event_and_audit(ctx.db, "review.ticket_transitioned", aggregate_id=run_id,
                           event_payload={"run_id": run_id, "fence": fence, "ticket": key, "actor": actor,
                                          "status": "To Do", "target": "To Do"},
                           target="jira", action="transition", ticket=key, run_id=run_id, fence=fence,
                           actor=actor, result="To Do", payload={"to": "To Do", "source": "human-review"})
    assert_event_and_audit(ctx.db, "review.labels_updated", aggregate_id=run_id,
                           event_payload={"run_id": run_id, "fence": fence, "ticket": key, "actor": actor,
                                          "add": ["rework"], "remove": []},
                           target="jira", action="fields", ticket=key, run_id=run_id, fence=fence,
                           actor=actor, result="review labels updated",
                           payload={"source": "human-review", "add": ["rework"], "remove": []})
    assert_event_and_audit(ctx.db, "review.action_accepted", aggregate_id=run_id,
                           event_payload={"run_id": run_id, "fence": fence, "ticket": key, "action": "rework",
                                          "kind": "review_before_merge", "text": "Please add coverage for retries."},
                           target="human-review", action="rework", ticket=key, run_id=run_id, fence=fence,
                           actor="reviewer-console", result="accepted",
                           payload={"kind": "review_before_merge", "text": "Please add coverage for retries."})


def test_approve_publishes_merge_audit_and_refused_merge_keeps_direct_refusal_audit(client, ctx, monkeypatch):
    key = "TESTWP-4782"
    add_review_ticket(ctx, key)
    add_ticket(ctx.db, "TESTWP-4783", status="Done")
    route_deps.github_connection_used.set("")

    response = client.post(f"/v1/reviews/{key}/approve", headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200, response.text
    run = ctx.db.one("SELECT id,fence,account_id FROM runs WHERE ticket_key=%s AND account_id='reviewer-console'",
                     (key,))
    run_id, fence, actor = str(run["id"]), run["fence"], run["account_id"]
    assert_event_and_audit(ctx.db, "github.merge_completed", aggregate_id=run_id,
                           event_payload={"run_id": run_id, "fence": fence, "ticket": key, "actor": actor,
                                          "number": 42, "method": "squash", "repo": "jooservices/test-aiflow-wp",
                                          "result": "abc123"},
                           target="github", action="merge", ticket=key, run_id=run_id, fence=fence,
                           actor=actor, result="abc123",
                           payload={"number": 42, "method": "squash", "repo": "jooservices/test-aiflow-wp"})

    refused_key = "TESTWP-4784"
    add_review_ticket(ctx, refused_key)
    monkeypatch.setattr(ctx.github, "merge_pr", lambda *_args, **_kwargs:
                        {"merged": False, "message": "required checks are pending"})
    refused = client.post(f"/v1/reviews/{refused_key}/approve",
                          headers={"Authorization": "Bearer review-secret"})
    assert refused.status_code == 200 and refused.json()["merged"] is False
    assert_event_absent(ctx.db, "github.merge_completed", ticket=refused_key)
    refused_run = ctx.db.one("SELECT id,fence,account_id FROM runs WHERE ticket_key=%s"
                             " AND account_id='reviewer-console'", (refused_key,))
    refused_rows = ctx.db.all(f"SELECT {AUDIT_COLUMNS} FROM write_audit WHERE target='github' AND action='merge'"
                              " AND ticket=%s", (refused_key,))
    for row in refused_rows:
        assert row["payload"].pop("connection", "default") == "default"
    assert refused_rows == [{
        "target": "github", "action": "merge", "ticket": refused_key, "run_id": str(refused_run["id"]),
        "fence": refused_run["fence"], "actor": refused_run["account_id"], "accepted": False,
        "result": "required checks are pending",
        "payload": {"number": 42, "method": "squash", "repo": "jooservices/test-aiflow-wp"}}]


@pytest.fixture
def flow_bug_maintenance_project(ctx):
    ctx.settings.maintenance_project = "TESTFLOW"


def join(client, name):
    response = join_node(client, name)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def ready(client, node, account):
    headers = join(client, node)
    response = client.post(f"/v1/nodes/{node}/heartbeat", json={
        "workers": [{"account": account, "state": "idle"}], "slots_total": 2,
        "metrics": {"hostname": node, "load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 20,
                    "disk_total": 100 * 1024 ** 3, "disk_free": 20 * 1024 ** 3, "disk_free_pct": 20},
    }, headers=headers)
    assert response.status_code == 200
    return headers


def claim_master(client, headers, account):
    response = client.post("/v1/work/claim", json={"ticket": MASTER_SCOPE, "account": account, "step": "master"},
                           headers=headers)
    assert response.status_code == 200
    return response.json()


def failed_flow_run(ctx, ticket, summary, *, step="dev", error_kind="infra", outcome="failed", node="node1"):
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,summary)"
               " VALUES (%s,%s,'TESTFLOW',%s,'codex-01','codex',%s,1,'finished',%s,%s,%s)",
               (run_id, ticket, step, node, outcome, error_kind, summary))
    return run_id


def scan_flow_bugs(client, headers, master):
    context = client.get("/v1/master/context", params={"run_id": master["run_id"], "fence": master["fence"]},
                         headers=headers)
    assert context.status_code == 200, context.text
    return client.post("/v1/master/flow-bugs", json={"run_id": master["run_id"], "fence": master["fence"]},
                       headers=headers)


def test_flow_bug_creation_repeat_and_intake_move_publish_matching_audits(client, ctx,
                                                                          flow_bug_maintenance_project):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01")
    summary = "Git refused checkout of branch TESTFLOW-4781: pathspec did not match"
    first_ids = [
        failed_flow_run(ctx, "TESTFLOW-4781", summary, step="triage"),
        failed_flow_run(ctx, "TESTFLOW-4781", summary, step="verify"),
        failed_flow_run(ctx, "TESTFLOW-4781", summary, step="triage"),
        failed_flow_run(ctx, "TESTFLOW-4782", "Git refused checkout of branch TESTFLOW-4782: pathspec did not match",
                        step="verify"),
    ]
    invalid = client.post("/v1/master/flow-bugs", json={}, headers=cc)
    assert invalid.status_code == 422

    created_response = scan_flow_bugs(client, cc, master)
    assert created_response.status_code == 200, created_response.text
    report = created_response.json()["reports"][0]
    assert report["action"] == "created"
    ticket = report["ticket"]
    created = ctx.db.one("SELECT seq,type,aggregate,aggregate_id,payload FROM domain_events"
                         " WHERE type='flow_bug.issue_created' ORDER BY seq")
    assert created["aggregate"] == "run" and created["aggregate_id"] == master["run_id"]
    created_payload = created["payload"]
    assert created_payload == {"run_id": master["run_id"], "fence": master["fence"], "ticket": ticket,
                              "actor": "claude-01",
                              "reason": "at least three matching failed runs across at least two tickets in 30 minutes",
                              "signature": report["signature"], "run_ids": sorted(first_ids),
                              "tickets": ["TESTFLOW-4781", "TESTFLOW-4782"], "labels": ["flow-bug"]}
    assert_event_and_audit(ctx.db, "flow_bug.issue_created", aggregate_id=master["run_id"],
                           event_payload=created_payload, target="jira", action="flow_bug_create", ticket=ticket,
                           run_id=master["run_id"], fence=master["fence"], actor="claude-01", result="created",
                           payload={"reason": created_payload["reason"], "signature": report["signature"],
                                    "run_ids": sorted(first_ids), "tickets": ["TESTFLOW-4781", "TESTFLOW-4782"],
                                    "labels": ["flow-bug"]})
    assert_event_and_audit(ctx.db, "flow_bug.intake_moved", aggregate_id=master["run_id"],
                           event_payload={"run_id": master["run_id"], "fence": master["fence"], "ticket": ticket,
                                          "actor": "claude-01", "signature": report["signature"]},
                           target="jira", action="flow_bug_intake", ticket=ticket, run_id=master["run_id"],
                           fence=master["fence"], actor="claude-01", result="Intake",
                           payload={"signature": report["signature"]})

    new_ids = [
        failed_flow_run(ctx, "TESTFLOW-4783", "Git refused checkout of branch TESTFLOW-4783: pathspec did not match",
                        step="verify"),
        failed_flow_run(ctx, "TESTFLOW-4784", "Git refused checkout of branch TESTFLOW-4784: pathspec did not match",
                        step="triage"),
    ]
    repeated_response = scan_flow_bugs(client, cc, master)
    assert repeated_response.status_code == 200, repeated_response.text
    repeated = repeated_response.json()["reports"][0]
    assert repeated["action"] == "repeated" and repeated["ticket"] == ticket
    repeat_event = ctx.db.one("SELECT seq,type,aggregate,aggregate_id,payload FROM domain_events"
                              " WHERE type='flow_bug.repeat_commented' ORDER BY seq")
    assert repeat_event["aggregate"] == "run" and repeat_event["aggregate_id"] == master["run_id"]
    assert sorted(repeated["new_run_ids"]) == sorted(new_ids)
    repeat_payload = {"run_id": master["run_id"], "fence": master["fence"], "ticket": ticket,
                      "actor": "claude-01", "comment_id": "1", "signature": report["signature"],
                      "repeat_count": 2, "new_run_ids": repeated["new_run_ids"]}
    assert repeat_event["payload"] == repeat_payload
    assert_event_and_audit(ctx.db, "flow_bug.repeat_commented", aggregate_id=master["run_id"],
                           event_payload=repeat_payload, target="jira", action="flow_bug_repeat", ticket=ticket,
                           run_id=master["run_id"], fence=master["fence"], actor="claude-01", result="1",
                           payload={"signature": report["signature"], "repeat_count": 2,
                                    "new_run_ids": repeated["new_run_ids"]})


def test_failed_intake_move_keeps_refusal_audit_and_retry_publishes_event(client, ctx,
                                                                          flow_bug_maintenance_project):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01")
    for key in ("TESTFLOW-4785", "TESTFLOW-4786", "TESTFLOW-4785"):
        failed_flow_run(ctx, key, f"Git refused checkout of branch {key}: pathspec did not match", step="triage")
    real_transition = ctx.jira.transition_to
    calls = []

    def fail_first_transition(key, target):
        calls.append((key, target))
        if len(calls) == 1:
            raise RuntimeError("Jira unavailable")
        return real_transition(key, target)

    ctx.jira.transition_to = fail_first_transition
    first = scan_flow_bugs(client, cc, master)
    assert first.status_code == 200 and first.json()["reports"][0]["in_intake"] is False
    ticket = first.json()["reports"][0]["ticket"]
    assert_event_absent(ctx.db, "flow_bug.intake_moved", ticket=ticket)
    failed_audit = ctx.db.one(f"SELECT {AUDIT_COLUMNS} FROM write_audit WHERE target='jira'"
                              " AND action='flow_bug_intake' AND ticket=%s", (ticket,))
    assert failed_audit == {"target": "jira", "action": "flow_bug_intake", "ticket": ticket,
                            "run_id": master["run_id"], "fence": master["fence"], "actor": "claude-01",
                            "accepted": False, "result": "Intake transition failed",
                            "payload": {"signature": first.json()["reports"][0]["signature"], "error": "RuntimeError"}}

    retried = scan_flow_bugs(client, cc, master)
    assert retried.status_code == 200 and retried.json()["reports"][0]["action"] == "moved_to_intake"
    signature = first.json()["reports"][0]["signature"]
    assert_event_and_audit(ctx.db, "flow_bug.intake_moved", aggregate_id=master["run_id"],
                           event_payload={"run_id": master["run_id"], "fence": master["fence"], "ticket": ticket,
                                          "actor": "claude-01", "signature": signature},
                           target="jira", action="flow_bug_intake", ticket=ticket, run_id=master["run_id"],
                           fence=master["fence"], actor="claude-01", result="Intake",
                           payload={"signature": signature})
