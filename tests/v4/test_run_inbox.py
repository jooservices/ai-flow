from __future__ import annotations

from uuid import uuid4

from conftest import add_ticket, join_node


def _claim(client, ctx, key: str):
    joined = join_node(client, "cc")
    node = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    add_ticket(ctx.db, key)
    claimed = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01"}, headers=node)
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    return node, claimed.json()


def test_run_inbox_is_ordered_idempotent_and_delivered_by_worker_heartbeat(client, ctx):
    node, run = _claim(client, ctx, "TESTWP-INBOX-1")
    client_id = str(uuid4())
    first = client.post(f"/v1/runs/{run['run_id']}/inbox",
                        json={"client_id": client_id, "kind": "request", "body": "Please add a regression test."})
    assert first.status_code == 201
    message = first.json()["message"]
    assert message["status"] == "queued" and message["author"] == "Console"

    duplicate = client.post(f"/v1/runs/{run['run_id']}/inbox",
                            json={"client_id": client_id, "kind": "idea", "body": "different retry payload"})
    assert duplicate.status_code == 201
    assert duplicate.json()["message"]["id"] == message["id"]
    assert duplicate.json()["message"]["body"] == "Please add a regression test."
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


def test_run_closure_marks_unconsumed_guidance_undelivered_and_rejects_late_messages(client, ctx):
    node, run = _claim(client, ctx, "TESTWP-INBOX-2")
    queued = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "idea", "body": "Consider a smaller patch."})
    assert queued.status_code == 201

    result = client.post(f"/v1/runs/{run['run_id']}/result", json={"fence": run["fence"], "outcome": "done"},
                         headers=node)
    assert result.status_code == 200
    history = client.get(f"/v1/runs/{run['run_id']}/inbox")
    assert history.json()["messages"][0]["status"] == "undelivered"
    late = client.post(f"/v1/runs/{run['run_id']}/inbox", json={"kind": "request", "body": "Too late"})
    assert late.status_code == 409


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
