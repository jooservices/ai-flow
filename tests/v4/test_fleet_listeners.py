"""A node join publishes NodeJoined; its telemetry row and its first readiness check are reactions (listeners)."""
from __future__ import annotations

from aiflow.v4.control import system_check_api


def join_without_helper(client, name):
    issued = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": name,
                                                         "role": "worker", "project": "TESTWP"})
    assert issued.status_code == 200, issued.text
    return client.post("/v1/nodes/join", json={"join_token": issued.json()["token"], "ip": ""})


def test_a_join_starts_the_readiness_check_and_records_the_telemetry_row_through_listeners(client, ctx, monkeypatch):
    started = []
    monkeypatch.setattr(system_check_api, "schedule_join_readiness", lambda *args: started.append(args))

    joined = join_without_helper(client, "listener-node")

    assert joined.status_code == 200, joined.text
    assert [(args[1]) for args in started] == ["listener-node"]
    telemetry = [e for e in client.get("/v1/state").json()["events"] if e["type"] == "node.joined"]
    assert [e["node"] for e in telemetry] == ["listener-node"]
    deliveries = ctx.db.all("SELECT listener,status FROM domain_event_deliveries d JOIN domain_events e ON e.id=d.event_id"
                            " WHERE e.type='node.agent_joined' ORDER BY listener")
    assert deliveries == [{"listener": "fleet.schedule_join_readiness", "status": "done"}]


def test_a_failing_readiness_start_does_not_fail_the_join_and_is_left_for_a_retry(client, ctx, monkeypatch):
    def failing(*_args):
        raise RuntimeError("provider probe could not start")

    monkeypatch.setattr(system_check_api, "schedule_join_readiness", failing)

    joined = join_without_helper(client, "listener-retry-node")

    assert joined.status_code == 200, joined.text
    row = ctx.db.one("SELECT d.status,d.attempts FROM domain_event_deliveries d JOIN domain_events e ON e.id=d.event_id"
                     " WHERE e.type='node.agent_joined' AND d.listener='fleet.schedule_join_readiness'")
    assert row["status"] == "pending" and row["attempts"] == 1
    assert ctx.db.one("SELECT status FROM nodes WHERE name='listener-retry-node'") == {"status": "joined"}
