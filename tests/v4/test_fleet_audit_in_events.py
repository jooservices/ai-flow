"""Successful fleet audit_in writes publish audited domain events in their state-change transactions."""
from __future__ import annotations

import pytest

from aiflow.v4.control.features.fleet import routes as fleet_routes
from conftest import join_node
from test_node_operations import sign_in


def assert_event_and_audit(db, event_type, *, aggregate, aggregate_id, event_payload, target, action, ticket, actor,
                           result, payload, run_id="", fence=None):
    events = db.all("SELECT seq,type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s ORDER BY seq",
                    (event_type,))
    assert [(row["type"], row["aggregate"], row["aggregate_id"], row["payload"]) for row in events] == [
        (event_type, aggregate, aggregate_id, event_payload)]

    rows = db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                  "WHERE target=%s AND action=%s AND ticket=%s AND actor=%s AND result=%s",
                  (target, action, ticket, actor, result))
    assert rows == [{"target": target, "action": action, "ticket": ticket, "run_id": run_id, "fence": fence,
                     "actor": actor, "accepted": True, "result": result, "payload": payload}]


def add_account(client, node):
    response = client.post(f"/v1/machines/{node}/accounts", json={
        "provider": "claude", "name": "Fleet Test", "sessions": 2, "jobs": ["develop", "verify"],
    })
    assert response.status_code == 200, response.text
    return response.json()["account"]


def test_node_join_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "fleet-agent-audit-node"
    joined = join_node(client, node, ip="192.0.2.20")
    assert joined.status_code == 200, joined.text

    assert_event_and_audit(db, "node.agent_joined", aggregate="node", aggregate_id=node,
                           event_payload={"node": node, "ip_present": True, "role": "worker", "project": "TESTWP"},
                           target="node", action="join", ticket=node, actor="node-agent", result="node joined",
                           payload={"ip_present": True, "role": "worker", "project": "TESTWP"})


def test_add_managed_account_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "fleet-account-add-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)

    account = add_account(client, node)

    payload = {"node": node, "provider": "claude", "name": "Fleet Test", "sessions": 2,
               "jobs": ["develop", "verify"]}
    assert_event_and_audit(db, "ai_account.added", aggregate="ai_account", aggregate_id=account,
                           event_payload={"account": account, "actor": "owner@example.test", **payload},
                           target="ai-account", action="add", ticket=account, actor="owner@example.test",
                           result="account added; sign-in pending", payload=payload)


def test_update_managed_account_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "fleet-account-update-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)
    account = add_account(client, node)

    response = client.patch(f"/v1/accounts/{account}", json={"action": "pause"})
    assert response.status_code == 200, response.text

    payload = {"sessions": 2, "jobs": ["develop", "verify"], "paused": True}
    assert_event_and_audit(db, "ai_account.management_changed", aggregate="ai_account", aggregate_id=account,
                           event_payload={"account": account, "actor": "owner@example.test", "action": "pause",
                                          **payload},
                           target="ai-account", action="pause", ticket=account, actor="owner@example.test",
                           result="updated", payload=payload)


def test_restart_managed_account_sign_in_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "fleet-account-restart-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)
    account = add_account(client, node)

    response = client.post(f"/v1/accounts/{account}/sign-in")
    assert response.status_code == 200, response.text

    payload = {"provider": "claude", "node": node}
    assert_event_and_audit(db, "ai_account.sign_in_restarted", aggregate="ai_account", aggregate_id=account,
                           event_payload={"account": account, "actor": "owner@example.test", **payload},
                           target="ai-account", action="sign_in_again", ticket=account, actor="owner@example.test",
                           result="provider sign-in queued on the machine", payload=payload)


def test_complete_managed_account_sign_in_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "fleet-account-complete-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)
    account = add_account(client, node)
    ctx.db.run("UPDATE managed_ai_accounts SET sign_in_status='completed',"
               "sign_in_started_at=now()-interval '1 minute' WHERE id=%s", (account,))
    ctx.db.run("UPDATE accounts SET status='idle',last_probe_at=now() WHERE id=%s", (account,))

    response = client.post(f"/v1/accounts/{account}/sign-in/complete")
    assert response.status_code == 200, response.text

    payload = {"node": node, "provider": "claude"}
    assert_event_and_audit(db, "ai_account.sign_in_completed", aggregate="ai_account", aggregate_id=account,
                           event_payload={"account": account, "actor": "owner@example.test", **payload},
                           target="ai-account", action="sign_in_complete", ticket=account,
                           actor="owner@example.test", result="provider account check succeeded", payload=payload)


def test_remove_managed_account_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "fleet-account-remove-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)
    account = add_account(client, node)

    response = client.request("DELETE", f"/v1/accounts/{account}", json={"confirm": True})
    assert response.status_code == 200, response.text

    payload = {"node": node}
    assert_event_and_audit(db, "ai_account.removed", aggregate="ai_account", aggregate_id=account,
                           event_payload={"account": account, "actor": "owner@example.test", **payload},
                           target="ai-account", action="remove", ticket=account, actor="owner@example.test",
                           result="account removed from machine", payload=payload)


def test_remove_managed_node_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "fleet-remove-audit-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)

    response = client.request("DELETE", f"/v1/nodes/{node}", json={"confirm": True})
    assert response.status_code == 200, response.text

    assert_event_and_audit(db, "node.managed_removed", aggregate="node", aggregate_id=node,
                           event_payload={"node": node, "actor": "owner@example.test"},
                           target="node", action="remove", ticket=node, actor="owner@example.test",
                           result="machine removed", payload={})


def test_node_operation_publishes_one_event_and_the_same_audit_row(client, ctx, db):
    node = "node1"
    joined = join_node(client, node)
    assert joined.status_code == 200, joined.text
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    ctx.repo().versions["tools"]["codex"] = "0.4.1"
    heartbeat = client.post(f"/v1/nodes/{node}/heartbeat", json={"tools": {"codex": "missing"}}, headers=headers)
    assert heartbeat.status_code == 200, heartbeat.text
    sign_in(client, ctx, role="super_admin")

    response = client.post(f"/v1/nodes/{node}/operations", json={"action": "install", "component": "codex"})
    assert response.status_code == 202, response.text
    operation_id = response.json()["id"]
    components = response.json()["components"]
    operation_payload = db.one("SELECT payload FROM node_operations WHERE id=%s", (operation_id,))["payload"]
    service_count = len(operation_payload["services"])
    audit_payload = {"operation_id": operation_id, "components": components, "service_count": service_count}

    assert_event_and_audit(db, "node.operation_queued", aggregate="node", aggregate_id=node,
                           event_payload={"node": node, "actor": "owner@example.test", "action": "install",
                                          "operation_id": operation_id, "components": components,
                                          "service_count": service_count},
                           target="node", action="install", ticket=node, actor="owner@example.test",
                           result="queued", payload=audit_payload, run_id=operation_id, fence=None)


def test_add_managed_account_rolls_back_when_event_publish_fails(client, ctx, db, monkeypatch):
    node = "fleet-account-rollback-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)

    def fail_publish(*args, **kwargs):
        raise RuntimeError("forced publish failure")

    monkeypatch.setattr(fleet_routes, "publish_for", fail_publish)
    with pytest.raises(RuntimeError, match="forced publish failure"):
        client.post(f"/v1/machines/{node}/accounts", json={
            "provider": "claude", "name": "Fleet Test", "sessions": 2, "jobs": ["develop", "verify"],
        })

    assert db.one("SELECT count(*) AS n FROM managed_ai_accounts WHERE node=%s", (node,)) == {"n": 0}
