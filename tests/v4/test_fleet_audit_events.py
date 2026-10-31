"""Accepted fleet audit writes are emitted as domain events and kept in the same transaction."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiflow.v4.control import gateway_helpers, route_deps
from conftest import join_node
from test_node_operations import publish_release, sign_in


ROOT = Path(__file__).resolve().parents[2]


def assert_event_and_audit(db, event_type, *, aggregate_id, event_payload, target, action, ticket, actor,
                           result, payload):
    events = db.all("SELECT type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s", (event_type,))
    assert events == [{"type": event_type, "aggregate": "node", "aggregate_id": aggregate_id,
                       "payload": event_payload}]
    rows = db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                  "WHERE target=%s AND action=%s AND result=%s", (target, action, result))
    assert rows == [{"target": target, "action": action, "ticket": ticket, "run_id": "", "fence": None,
                     "actor": actor, "accepted": True, "result": result, "payload": payload}]


def install_probe_gateway(monkeypatch, gateway, connection):
    monkeypatch.setattr(gateway_helpers, "github_route",
                        lambda ctx, repo: SimpleNamespace(id=connection, gateway=gateway))


def probe_node(client, name="fleet-probe-node"):
    joined = join_node(client, name)
    assert joined.status_code == 200, joined.text
    return {"Authorization": f"Bearer {joined.json()['node_token']}"}


def test_create_join_command_publishes_one_event_and_writes_the_same_audit_row(
        client, ctx, db, tmp_path, monkeypatch):
    publish_release(tmp_path, monkeypatch)
    sign_in(client, ctx)
    route_deps.github_connection_used.set("")

    response = client.post("/v1/nodes/fleet-join-node/join-command", json={
        "name": "fleet-join-node", "role": "worker", "project": "TESTWP",
    })

    assert response.status_code == 200, response.text
    data = response.json()
    event_payload = {"node": "fleet-join-node", "actor": "owner@example.test", "expires_at": data["expires_at"],
                     "role": "worker", "project": "TESTWP"}
    audit_payload = {"expires_at": data["expires_at"], "role": "worker", "project": "TESTWP",
                     "ssh_password_saved": False}
    assert_event_and_audit(db, "node.join_command_created", aggregate_id="fleet-join-node",
                           event_payload=event_payload, target="node", action="join_command",
                           ticket="fleet-join-node", actor="owner@example.test",
                           result="one-time join command created", payload=audit_payload)


def test_set_node_work_acceptance_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    joined = join_node(client, "fleet-work-node")
    assert joined.status_code == 200, joined.text
    sign_in(client, ctx)
    route_deps.github_connection_used.set("")

    response = client.put("/v1/nodes/fleet-work-node/work-acceptance?accepting_work=false", json={"confirm": True})

    assert response.status_code == 200, response.text
    assert_event_and_audit(db, "node.work_acceptance_changed", aggregate_id="fleet-work-node",
                           event_payload={"node": "fleet-work-node", "actor": "owner@example.test",
                                          "accepting_work": False},
                           target="node", action="stop_taking_work", ticket="fleet-work-node",
                           actor="owner@example.test", result="stopping new work", payload={})


def test_check_probe_token_relay_publishes_one_event_and_writes_the_same_audit_row(
        client, ctx, db, tmp_path, monkeypatch):
    ctx.settings.github_app_dir = str(tmp_path / "github-secrets")
    headers = probe_node(client)
    gateway = SimpleNamespace(kind="token")
    connection = "fleet-relay-connection"
    install_probe_gateway(monkeypatch, gateway, connection)
    route_deps.github_connection_used.set("")

    response = client.post("/v1/nodes/fleet-probe-node/check-probes/token",
                           json={"project": "TESTWP", "repo": "jooservices/test-aiflow-wp"}, headers=headers)

    assert response.status_code == 200, response.text
    expires = response.json()["expires_at"]
    fields = {"node": "fleet-probe-node", "repo": "jooservices/test-aiflow-wp", "project": "TESTWP",
              "connection": connection, "expires": expires, "transport": "control"}
    payload = {"repo": fields["repo"], "project": fields["project"], "connection": connection,
               "expires": expires, "transport": "control"}
    assert_event_and_audit(db, "github.check_probe_token_issued", aggregate_id="fleet-probe-node",
                           event_payload=fields, target="github", action="check_probe_token", ticket="",
                           actor="fleet-probe-node", result="read-only relay capability issued", payload=payload)


def test_check_probe_token_direct_publishes_one_event_and_writes_the_same_audit_row(
        client, ctx, db, monkeypatch):
    headers = probe_node(client)
    expires = datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc)

    class DirectGateway:
        kind = "app"

        def token(self, repo, permissions):
            return "check-probe-token-for-response-only", expires

    connection = "fleet-direct-connection"
    install_probe_gateway(monkeypatch, DirectGateway(), connection)
    route_deps.github_connection_used.set("")

    response = client.post("/v1/nodes/fleet-probe-node/check-probes/token",
                           json={"project": "TESTWP", "repo": "jooservices/test-aiflow-wp"}, headers=headers)

    assert response.status_code == 200, response.text
    expires_at = response.json()["expires_at"]
    fields = {"node": "fleet-probe-node", "repo": "jooservices/test-aiflow-wp", "project": "TESTWP",
              "connection": connection, "expires": expires_at, "transport": ""}
    payload = {"repo": fields["repo"], "project": fields["project"], "connection": connection,
               "expires": expires_at}
    assert_event_and_audit(db, "github.check_probe_token_issued", aggregate_id="fleet-probe-node",
                           event_payload=fields, target="github", action="check_probe_token", ticket="",
                           actor="fleet-probe-node", result="issued", payload=payload)


def test_work_acceptance_rolls_back_with_the_audit(client, ctx, db, monkeypatch):
    joined = join_node(client, "fleet-rollback-node")
    assert joined.status_code == 200, joined.text
    sign_in(client, ctx)
    before = db.one("SELECT accepting_work FROM nodes WHERE name='fleet-rollback-node'")
    assert before == {"accepting_work": True}

    def fail_audit(*args, **kwargs):
        from fastapi import HTTPException

        raise HTTPException(500, "forced audit failure")

    monkeypatch.setattr(route_deps, "audit", fail_audit)
    response = client.put("/v1/nodes/fleet-rollback-node/work-acceptance?accepting_work=false",
                          json={"confirm": True})

    assert response.status_code == 500
    assert db.one("SELECT accepting_work FROM nodes WHERE name='fleet-rollback-node'") == before
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='node.work_acceptance_changed'") == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='node' AND action='stop_taking_work'") == {
        "n": 0}


def test_a_refused_join_command_still_writes_its_refused_audit_row_and_no_event(client, ctx, db):
    joined = join_node(client, "fleet-refused-node")
    assert joined.status_code == 200, joined.text
    sign_in(client, ctx)

    response = client.put("/v1/nodes/fleet-refused-node/work-acceptance?accepting_work=false", json={})

    assert response.status_code == 400
    rows = db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                  "WHERE target='node' AND action='stop_taking_work'")
    assert rows == [{"target": "node", "action": "stop_taking_work", "ticket": "fleet-refused-node",
                     "run_id": "", "fence": None, "actor": "owner@example.test", "accepted": False,
                     "result": "confirmation required", "payload": {}}]
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='node.work_acceptance_changed'") == {"n": 0}


def test_the_fleet_routes_only_call_audit_for_refused_writes():
    routes = ROOT / "aiflow/v4/control/features/fleet/routes.py"
    module = ast.parse(routes.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(module)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "audit"]

    assert calls
    for call in calls:
        accepted = next((keyword.value for keyword in call.keywords if keyword.arg == "accepted"),
                        call.args[6] if len(call.args) >= 7 else None)
        assert isinstance(accepted, ast.Constant) and accepted.value is False
