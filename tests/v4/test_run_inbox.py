from __future__ import annotations

from uuid import uuid4

import pytest

from conftest import add_ticket, join_node


def _claim(client, ctx, key: str, node: dict | None = None):
    if node is None:
        joined = join_node(client, "cc")
        node = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    add_ticket(ctx.db, key)
    claimed = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01"}, headers=node)
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    return node, claimed.json()


def test_run_inbox_is_ordered_idempotent_and_delivered_by_worker_heartbeat(client, ctx):
    node, run = _claim(client, ctx, "TESTWP-INBOX-1")
    ticket_before = ctx.db.one("SELECT status,description FROM tickets WHERE key='TESTWP-INBOX-1'")
    jira_transitions_before = list(ctx.jira.transitions)
    client_id = str(uuid4())
    first = client.post(f"/v1/runs/{run['run_id']}/inbox",
                        json={"client_id": client_id, "kind": "request", "body": "Please add a regression test."})
    assert first.status_code == 201
    message = first.json()["message"]
    assert message["status"] == "queued" and message["author"] == "Console"
    assert ctx.db.one("SELECT inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))["inbox_pending_count"] == 1

    duplicate = client.post(f"/v1/runs/{run['run_id']}/inbox",
                            json={"client_id": client_id, "kind": "idea", "body": "different retry payload"})
    assert duplicate.status_code == 201
    assert duplicate.json()["message"]["id"] == message["id"]
    assert duplicate.json()["message"]["body"] == "Please add a regression test."
    assert ctx.db.one("SELECT inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))["inbox_pending_count"] == 1
    second = client.post(f"/v1/runs/{run['run_id']}/inbox",
                         json={"kind": "context", "body": "The old worktree must be preserved."}).json()["message"]

    heartbeat = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                            json={"fence": run["fence"], "progress": {"phase": "dev"}}, headers=node)
    assert heartbeat.status_code == 200
    assert [row["id"] for row in heartbeat.json()["inbox"]] == [message["id"], second["id"]]
    received = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"], "inbox_received": [message["id"], second["id"]]}, headers=node)
    assert received.status_code == 200 and [row["status"] for row in received.json()["inbox"]] == ["received", "received"]
    included = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"], "inbox_included": [message["id"], second["id"]]}, headers=node)
    assert included.status_code == 200 and included.json()["inbox"] == []

    history = client.get(f"/v1/runs/{run['run_id']}/inbox")
    assert history.status_code == 200
    assert [row["status"] for row in history.json()["messages"]] == ["included", "included"]
    assert history.json()["messages"][0]["body"] == "Please add a regression test."
    assert ctx.db.one("SELECT inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))["inbox_pending_count"] == 0

    audit = ctx.db.all("SELECT type,ticket,project,run_id,fence,node,outcome,data FROM events"
                       " WHERE run_id=%s AND type LIKE 'run.inbox.%%' ORDER BY id", (run["run_id"],))
    assert [event["type"] for event in audit] == ["run.inbox.queued", "run.inbox.queued",
                                                   "run.inbox.received", "run.inbox.included"]
    for event in audit:
        assert (event["ticket"], event["project"], event["run_id"], event["fence"], event["node"]) == (
            "TESTWP-INBOX-1", "TESTWP", run["run_id"], run["fence"], "cc")
    assert [event["data"]["message_id"] for event in audit[:2]] == [message["id"], second["id"]]
    assert audit[2]["outcome"] == "received" and audit[2]["data"]["message_ids"] == [message["id"], second["id"]]
    assert audit[3]["outcome"] == "included" and audit[3]["data"]["message_ids"] == [message["id"], second["id"]]

    ticket_after = ctx.db.one("SELECT status,description FROM tickets WHERE key='TESTWP-INBOX-1'")
    assert ticket_after == ticket_before  # run guidance is audited separately from ticket acceptance criteria
    assert ctx.jira.transitions == jira_transitions_before


def test_heartbeat_saves_progress_and_renews_the_run_lease(client, ctx, monkeypatch):
    node, run = _claim(client, ctx, "TESTWP-HEARTBEAT-PROGRESS")
    renew = ctx.leases.renew
    renewals = []

    def record_renewal(ticket, run_id, fence, ttl_ms):
        result = renew(ticket, run_id, fence, ttl_ms)
        renewals.append((ticket, run_id, fence, ttl_ms, result))
        return result

    monkeypatch.setattr(ctx.leases, "renew", record_renewal)
    before = ctx.db.one("SELECT heartbeat_at FROM runs WHERE id=%s", (run["run_id"],))["heartbeat_at"]

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"], "progress": {"phase": "verify", "percent": 40}},
                           headers=node)

    assert response.status_code == 200
    assert response.json()["lease_ms"] == ctx.settings.lease_ms
    assert renewals == [("TESTWP-HEARTBEAT-PROGRESS", run["run_id"], run["fence"],
                         ctx.settings.lease_ms, True)]
    saved = ctx.db.one("SELECT heartbeat_at,progress FROM runs WHERE id=%s", (run["run_id"],))
    assert saved["heartbeat_at"] is not None and saved["heartbeat_at"] >= before
    assert saved["progress"]["phase"] == "verify" and saved["progress"]["percent"] == 40
    assert "reported_at" in saved["progress"]


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-node-token"])
def test_heartbeat_requires_a_valid_node_token(client, ctx, authorization):
    _node, run = _claim(client, ctx, "TESTWP-HEARTBEAT-AUTH")
    headers = {} if authorization is None else {"Authorization": authorization}

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat", json={"fence": run["fence"]}, headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize("run_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_heartbeat_rejects_malformed_and_unknown_run_ids(client, ctx, run_id):
    node, _run = _claim(client, ctx, "TESTWP-HEARTBEAT-UNKNOWN")

    response = client.post(f"/v1/runs/{run_id}/heartbeat", json={"fence": 1}, headers=node)

    assert response.status_code == 404


def test_heartbeat_rejects_another_node_and_a_non_running_run(client, ctx):
    node, run = _claim(client, ctx, "TESTWP-HEARTBEAT-OWNER")
    other = join_node(client, "other-worker")
    wrong_node = {"Authorization": f"Bearer {other.json()['node_token']}"}
    denied = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                         json={"fence": run["fence"]}, headers=wrong_node)
    assert denied.status_code == 403

    finished = client.post(f"/v1/runs/{run['run_id']}/result",
                           json={"fence": run["fence"], "outcome": "done"}, headers=node)
    assert finished.status_code == 200
    inactive = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"]}, headers=node)
    assert inactive.status_code == 409


@pytest.mark.parametrize("field", ["inbox_received", "inbox_included"])
@pytest.mark.parametrize(("size", "status"), [(100, 200), (101, 422)])
def test_heartbeat_inbox_fields_enforce_the_100_item_boundary(client, ctx, field, size, status):
    node, run = _claim(client, ctx, f"TESTWP-HEARTBEAT-{field}-{size}")
    body = {"fence": run["fence"], field: list(range(size))}

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat", json=body, headers=node)

    assert response.status_code == status


def test_stale_fence_loses_the_run_and_closes_pending_inbox(client, ctx):
    node, run = _claim(client, ctx, "TESTWP-HEARTBEAT-STALE-FENCE")
    queued = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "context", "body": "handoff"})
    assert queued.status_code == 201
    message_id = queued.json()["message"]["id"]

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"] + 1}, headers=node)

    assert response.status_code == 409
    saved = ctx.db.one("SELECT status,inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))
    assert saved == {"status": "lost", "inbox_pending_count": 0}
    history = client.get(f"/v1/runs/{run['run_id']}/inbox").json()["messages"]
    assert [(message["id"], message["status"]) for message in history] == [(message_id, "undelivered")]
    event_types = ctx.db.all("SELECT type FROM events WHERE run_id=%s ORDER BY id", (run["run_id"],))
    assert [event["type"] for event in event_types if event["type"] in
            ("run.inbox.undelivered", "run.lost")] == ["run.inbox.undelivered", "run.lost"]


def test_lost_lease_heartbeat_cleans_up_pending_inbox_and_emits_events(client, ctx):
    node, run = _claim(client, ctx, "TESTWP-HEARTBEAT-LOST-LEASE")
    queued = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "context", "body": "handoff"})
    assert queued.status_code == 201
    message_id = queued.json()["message"]["id"]
    assert ctx.leases.release("TESTWP-HEARTBEAT-LOST-LEASE", run["run_id"], run["fence"])

    response = client.post(f"/v1/runs/{run['run_id']}/heartbeat",
                           json={"fence": run["fence"]}, headers=node)

    assert response.status_code == 409
    saved = ctx.db.one("SELECT status,inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))
    assert saved == {"status": "lost", "inbox_pending_count": 0}
    history = client.get(f"/v1/runs/{run['run_id']}/inbox").json()["messages"]
    assert [(message["id"], message["status"]) for message in history] == [(message_id, "undelivered")]
    event_types = ctx.db.all("SELECT type FROM events WHERE run_id=%s ORDER BY id", (run["run_id"],))
    assert [event["type"] for event in event_types if event["type"] in
            ("run.inbox.undelivered", "run.lost")] == ["run.inbox.undelivered", "run.lost"]


def test_run_closure_marks_unconsumed_guidance_undelivered_and_rejects_late_messages(client, ctx):
    node, run = _claim(client, ctx, "TESTWP-INBOX-2")
    queued = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "idea", "body": "Consider a smaller patch."})
    assert queued.status_code == 201

    result = client.post(f"/v1/runs/{run['run_id']}/result", json={"fence": run["fence"], "outcome": "done"},
                         headers=node)
    assert result.status_code == 200
    assert ctx.db.one("SELECT inbox_pending_count FROM runs WHERE id=%s", (run["run_id"],))["inbox_pending_count"] == 0
    history = client.get(f"/v1/runs/{run['run_id']}/inbox")
    assert history.json()["messages"][0]["status"] == "undelivered"
    late = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "request", "body": "Too late"})
    assert late.status_code == 409


def test_lost_run_accepts_late_usage_once_without_reopening_or_resetting_account(client, ctx):
    from aiflow.v4.control.loops import watchdog

    node, run = _claim(client, ctx, "TESTWP-INBOX-LATE-RESULT")
    queued = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "context", "body": "Keep this branch."})
    assert queued.status_code == 201
    ctx.leases.release("TESTWP-INBOX-LATE-RESULT", run["run_id"], run["fence"])

    watchdog(ctx)
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (run["run_id"],))["status"] == "lost"
    assert ctx.db.one("SELECT status FROM accounts WHERE id=%s", (run["account"],))["status"] == "idle"
    replacement_node, replacement = _claim(client, ctx, "TESTWP-INBOX-REPLACEMENT", node=node)
    assert replacement["account"] == run["account"]

    result_body = {"fence": run["fence"], "outcome": "done", "tokens_in": 17,
                   "tokens_out": 9, "cost_usd": 0.42, "summary": "The worker finished after losing its lease."}
    stale_fence = {**result_body, "fence": run["fence"] + 1}
    assert client.post(f"/v1/runs/{run['run_id']}/result", json=stale_fence, headers=replacement_node).status_code == 409
    account_status_before = ctx.db.one("SELECT status FROM accounts WHERE id=%s", (run["account"],))["status"]
    late = client.post(f"/v1/runs/{run['run_id']}/result", json=result_body, headers=replacement_node)

    assert late.status_code == 200 and not late.json()["still_owner"]
    saved = ctx.db.one("SELECT status,outcome,tokens_in,tokens_out,cost_usd,result_received_at FROM runs WHERE id=%s",
                       (run["run_id"],))
    assert saved["status"] == "lost" and saved["outcome"] == "lost"
    assert saved["tokens_in"] == 17 and saved["tokens_out"] == 9 and float(saved["cost_usd"]) == 0.42
    assert saved["result_received_at"] is not None
    assert ctx.db.one("SELECT status FROM accounts WHERE id=%s", (run["account"],))["status"] == account_status_before
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (replacement["run_id"],))["status"] == "running"
    assert client.get(f"/v1/runs/{run['run_id']}/inbox").json()["messages"][0]["status"] == "undelivered"
    assert client.post(f"/v1/runs/{run['run_id']}/result", json=result_body, headers=replacement_node).status_code == 409
    after_duplicate = ctx.db.one("SELECT tokens_in,tokens_out,cost_usd FROM runs WHERE id=%s", (run["run_id"],))
    assert after_duplicate["tokens_in"] == 17 and after_duplicate["tokens_out"] == 9
    assert float(after_duplicate["cost_usd"]) == 0.42


def test_run_inbox_requires_an_allowed_console_role_and_project_scope(client, ctx):
    from fastapi.testclient import TestClient

    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session

    _node, run = _claim(client, ctx, "TESTWP-INBOX-SCOPE")
    email = "delivery@example.test"
    secret = "test-console-session-secret-at-least-32-bytes"
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = secret
    ctx.db.run("INSERT INTO console_users (email,display_name,role,roles,project_keys,password_hash)"
               " VALUES (%s,'Delivery','delivery',%s,%s,'test-hash')",
               (email, ["delivery"], ["OTHER"]))
    scoped_client = TestClient(create_app(ctx))
    scoped_client.cookies.set(COOKIE_NAME, issue_session(email, "delivery", 2_000_000_000, secret))

    denied = scoped_client.post(f"/v1/runs/{run['run_id']}/inbox", json={"body": "outside scope"})

    assert denied.status_code == 403
