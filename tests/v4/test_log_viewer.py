"""JAIF-175: administrator-only recent host and service logs with server-side credential redaction."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.ui import mount_ui
from aiflow.v4.core.log_sources import read_source
from conftest import join_node


SESSION_SECRET = "log-viewer-test-secret-with-at-least-32-bytes"
SESSION_EXPIRY = 2_000_000_000


def sign_in(client, ctx, role="administrator", subject="admin@example.test"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set(COOKIE_NAME, issue_session(subject, role, SESSION_EXPIRY, SESSION_SECRET))


def joined_control_node(client, name="log-control"):
    response = join_node(client, name, role="control")
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json()["node_token"]}
    heartbeat = client.post(f"/v1/nodes/{name}/heartbeat", json={}, headers=headers)
    assert heartbeat.status_code == 200
    return headers


def complete_log_request(client, node_headers, request_id, lines, available=True):
    operation = client.get("/v1/nodes/log-control/operations/next", headers=node_headers)
    assert operation.status_code == 200
    claimed = operation.json()["operation"]
    assert claimed and claimed["action"] == "read_logs"
    return client.post(f"/v1/nodes/log-control/operations/{request_id}/result", headers=node_headers,
                       json={"status": "completed", "result": {"available": available, "lines": lines}})


def test_administrator_reads_recent_lines_and_secrets_are_redacted_before_storage_and_response(client, ctx):
    node_headers = joined_control_node(client)
    sign_in(client, ctx)

    sources = client.get("/v1/log-sources")
    assert sources.status_code == 200
    source = next(item for item in sources.json()["sources"]
                  if item["id"] == "log-control:container:aiflow4-control-api-a")
    assert source["available"] is True and source["name"] == "Container · aiflow4-control-api-a"

    queued = client.post("/v1/logs", json={"source_id": source["id"]})
    assert queued.status_code == 202
    request_id = queued.json()["id"]
    raw_lines = ["server started", "password=sample-secret-value",
                 "Authorization: Bearer synthetic-bearer-credential-value",
                 "api_key=synthetic-api-key-value"]
    finished = complete_log_request(client, node_headers, request_id, raw_lines)
    assert finished.status_code == 200

    response = client.get(f"/v1/logs/requests/{request_id}")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed" and result["available"] is True
    assert result["lines"][0] == "server started"
    joined = "\n".join(result["lines"])
    for secret in ("sample-secret-value", "synthetic-bearer-credential-value", "synthetic-api-key-value"):
        assert secret not in joined
    assert joined.count("[REDACTED]") >= 3

    stored = ctx.db.one("SELECT result FROM node_operations WHERE id=%s", (request_id,))["result"]
    assert all(secret not in str(stored) for secret in
               ("sample-secret-value", "synthetic-bearer-credential-value", "synthetic-api-key-value"))
    # The general node-operation API is intentionally not a way for other console roles to fetch log output.
    client.cookies.set(COOKIE_NAME, issue_session("lead@example.test", "team_lead", SESSION_EXPIRY, SESSION_SECRET))
    operations = client.get("/v1/nodes/log-control/operations")
    assert operations.status_code == 200
    assert all(operation["action"] != "read_logs" for operation in operations.json()["operations"])
    refused = client.get(f"/v1/logs/requests/{request_id}")
    assert refused.status_code == 403 and "lines" not in refused.json()


def test_unauthenticated_and_disallowed_roles_get_no_log_lines(client, ctx):
    node_headers = joined_control_node(client)
    ctx.settings.console_session_secret = SESSION_SECRET

    for path in ("/v1/log-sources", "/v1/logs"):
        refused = client.get(path) if path.endswith("sources") else client.post(path, json={"source_id": "anything"})
        assert refused.status_code == 401 and "lines" not in refused.json()

    sign_in(client, ctx, "sa", "sa@example.test")
    assert client.get("/v1/log-sources").status_code == 403
    refused = client.post("/v1/logs", json={"source_id": "log-control:host:host"})
    assert refused.status_code == 403 and "lines" not in refused.json()


def test_unknown_log_source_is_refused_and_node_reader_does_not_run_arbitrary_targets(monkeypatch, client, ctx):
    joined_control_node(client)
    sign_in(client, ctx)
    response = client.post("/v1/logs", json={"source_id": "log-control:file:/etc/passwd"})
    assert response.status_code == 404 and response.json() == {"detail": "unknown log source"}

    called = []
    monkeypatch.setattr("aiflow.v4.core.log_sources.subprocess.run", lambda *args, **kwargs: called.append(args))
    assert read_source("unit", "ssh.service")["available"] is False
    assert read_source("container", "unlisted-container")["available"] is False
    assert called == []


def test_unavailable_and_empty_log_sources_have_clear_messages(client, ctx):
    node_headers = joined_control_node(client)
    sign_in(client, ctx)
    source_id = "log-control:container:aiflow4-control-api-a"

    unavailable_request = client.post("/v1/logs", json={"source_id": source_id})
    assert unavailable_request.status_code == 202
    request_id = unavailable_request.json()["id"]
    assert complete_log_request(client, node_headers, request_id, [], available=False).status_code == 200
    unavailable = client.get(f"/v1/logs/requests/{request_id}").json()
    assert unavailable["available"] is False and unavailable["lines"] == []
    assert unavailable["message"] == "This log source is unavailable right now."
    assert "Traceback" not in unavailable["message"]

    empty_request = client.post("/v1/logs", json={"source_id": source_id})
    assert empty_request.status_code == 202
    request_id = empty_request.json()["id"]
    assert complete_log_request(client, node_headers, request_id, [], available=True).status_code == 200
    empty = client.get(f"/v1/logs/requests/{request_id}").json()
    assert empty["available"] is True and empty["lines"] == []
    assert empty["message"] == "No recent log lines for this source."


def test_logs_screen_requires_an_authenticated_administrator(ctx):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SESSION_SECRET
    app = create_app(ctx)
    mount_ui(app, ctx)
    client = TestClient(app, follow_redirects=False)

    unauthenticated = client.get("/logs")
    assert unauthenticated.status_code == 302 and "/login?next=" in unauthenticated.headers["location"]
    client.cookies.set(COOKIE_NAME, issue_session("sa@example.test", "sa", SESSION_EXPIRY, SESSION_SECRET))
    refused = client.get("/logs")
    assert refused.status_code == 403 and "Host and service logs" not in refused.text
    client.cookies.set(COOKIE_NAME, issue_session("admin@example.test", "administrator", SESSION_EXPIRY, SESSION_SECRET))
    allowed = client.get("/logs")
    assert allowed.status_code == 200 and "Available sources" in allowed.text
