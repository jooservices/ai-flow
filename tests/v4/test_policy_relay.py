import logging

import httpx
from fastapi.testclient import TestClient

from aiflow.v4.node.policy_relay import PolicyRelay


def test_policy_relay_retries_and_forwards_hook_unchanged(caplog):
    attempts = []

    def control_api(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("control plane restarting", request=request)
        if len(attempts) == 2:
            return httpx.Response(503, json={"error": "restarting"})
        return httpx.Response(200, json={"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": "allowed"}})

    control_client = httpx.Client(
        base_url="http://control.test",
        transport=httpx.MockTransport(control_api),
    )
    relay = PolicyRelay("http://control.test", client=control_client, sleep=lambda _seconds: None)
    body = b'{ "tool_name": "Write", "tool_input": {"file_path":"/work/a.py"} }'
    caplog.set_level(logging.INFO, logger="aiflow4.node.policy_relay")

    with TestClient(relay.app) as client:
        response = client.post("/v1/policy/decide", content=body,
                               headers={"Authorization": "Bearer run-token", "Content-Type": "application/json"})

    assert response.status_code == 200
    assert response.json()["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(attempts) == 3
    assert all(request.content == body for request in attempts)
    assert all(request.headers["authorization"] == "Bearer run-token" for request in attempts)
    assert "policy decision tool=Write decision=allow reason=allowed" in caplog.text
    control_client.close()
