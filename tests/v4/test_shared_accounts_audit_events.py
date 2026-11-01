"""Successful shared-account and machine-load-limit writes publish their audit events."""
from __future__ import annotations

import time

from conftest import join_node
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session


ACTOR = "owner@example.test"
SESSION_SECRET = "test-console-session-secret-at-least-32-bytes"


def owner_session(client, ctx):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set(COOKIE_NAME, issue_session(ACTOR, "owner", int(time.time()) + 3600, SESSION_SECRET))


def assert_event_and_audit(ctx, event_type, *, aggregate, aggregate_id, event_payload, target, action, ticket,
                           actor, result, audit_payload=None):
    events = ctx.db.all("SELECT seq,type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s ORDER BY seq",
                        (event_type,))
    assert len(events) == 1
    assert {key: value for key, value in events[0].items() if key != "seq"} == {
        "type": event_type, "aggregate": aggregate, "aggregate_id": aggregate_id, "payload": event_payload}

    rows = ctx.db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                      "WHERE target=%s AND action=%s AND ticket=%s", (target, action, ticket))
    assert rows == [{"target": target, "action": action, "ticket": ticket, "run_id": "", "fence": None,
                     "actor": actor, "accepted": True, "result": result, "payload": audit_payload or {}}]


def create_account(client, account="shared-01"):
    # AccountBody requires id and provider; the other fields have defaults.
    response = client.post("/v1/shared-accounts", json={"id": account, "provider": "claude"})
    assert response.status_code == 201, response.text
    return response


def test_create_shared_account_publishes_event_and_matching_audit(client, ctx):
    owner_session(client, ctx)

    response = create_account(client)

    assert response.json()["id"] == "shared-01"
    assert_event_and_audit(ctx, "ai_account.created", aggregate="ai_account", aggregate_id="shared-01",
                           event_payload={"account": "shared-01", "actor": ACTOR}, target="account", action="create",
                           ticket="shared-01", actor=ACTOR, result="created")


def test_session_limit_write_publishes_event_and_matching_audit(client, ctx):
    owner_session(client, ctx)

    response = client.put("/v1/shared-accounts/claude-01/session-limit", json={"session_limit": 3})

    assert response.status_code == 200, response.text
    assert_event_and_audit(ctx, "ai_account.session_limit_set", aggregate="ai_account", aggregate_id="claude-01",
                           event_payload={"account": "claude-01", "actor": ACTOR, "session_limit": 3},
                           target="account", action="session_limit", ticket="claude-01", actor=ACTOR, result="3")


def test_enable_machine_publishes_event_and_matching_audit(client, ctx):
    owner_session(client, ctx)
    joined = join_node(client, "enable-node")
    assert joined.status_code == 200, joined.text

    response = client.post("/v1/shared-accounts/claude-01/machines", json={"node": "enable-node"})

    assert response.status_code == 201, response.text
    assert_event_and_audit(ctx, "ai_account.machine_enabled", aggregate="ai_account", aggregate_id="claude-01",
                           event_payload={"account": "claude-01", "actor": ACTOR, "node": "enable-node"},
                           target="account", action="enable_machine", ticket="claude-01", actor=ACTOR,
                           result="enable-node")


def test_retry_signin_publishes_event_and_matching_audit(client, ctx):
    owner_session(client, ctx)
    create_account(client)
    joined = join_node(client, "signin-node")
    assert joined.status_code == 200, joined.text
    enabled = client.post("/v1/shared-accounts/shared-01/machines", json={"node": "signin-node"})
    assert enabled.status_code == 201, enabled.text

    response = client.post("/v1/shared-accounts/shared-01/machines/signin-node/signin")

    assert response.status_code == 200, response.text
    assert_event_and_audit(ctx, "ai_account.sign_in_retried", aggregate="ai_account", aggregate_id="shared-01",
                           event_payload={"account": "shared-01", "actor": ACTOR, "node": "signin-node"},
                           target="account", action="retry_signin", ticket="shared-01", actor=ACTOR,
                           result="signin-node")


def test_remove_machine_publishes_event_and_matching_audit(client, ctx):
    owner_session(client, ctx)
    create_account(client)
    joined = join_node(client, "remove-node")
    assert joined.status_code == 200, joined.text
    enabled = client.post("/v1/shared-accounts/shared-01/machines", json={"node": "remove-node"})
    assert enabled.status_code == 201, enabled.text

    response = client.delete("/v1/shared-accounts/shared-01/machines/remove-node", params={"confirm": "true"})

    assert response.status_code == 200, response.text
    assert_event_and_audit(ctx, "ai_account.machine_removed", aggregate="ai_account", aggregate_id="shared-01",
                           event_payload={"account": "shared-01", "actor": ACTOR, "node": "remove-node",
                                          "account_removed": True},
                           target="account", action="remove_machine", ticket="shared-01", actor=ACTOR,
                           result="remove-node", audit_payload={"account_removed": True})


def test_remove_account_publishes_event_and_matching_audit(client, ctx):
    owner_session(client, ctx)
    create_account(client)

    response = client.delete("/v1/shared-accounts/shared-01", params={"confirm": "true"})

    assert response.status_code == 200, response.text
    assert_event_and_audit(ctx, "ai_account.removed_everywhere", aggregate="ai_account", aggregate_id="shared-01",
                           event_payload={"account": "shared-01", "actor": ACTOR, "source_retained": False},
                           target="account", action="remove", ticket="shared-01", actor=ACTOR,
                           result="removed from all machines", audit_payload={"source_retained": False})


def test_save_machine_load_limits_publishes_event_and_matching_audit(client, ctx):
    owner_session(client, ctx)
    joined = join_node(client, "load-node")
    assert joined.status_code == 200, joined.text
    machine = {"cpu_load_per_cpu": 0.9, "memory_used_pct": 90.0, "disk_free_pct": 8.0}

    response = client.put("/v1/machines/load-node/load-limits", json={
        "cpu_load_per_cpu": 0.9, "memory_used_pct": 90, "disk_free_pct": 8,
        "host_cpu_load_per_cpu": 0.75,
    })

    assert response.status_code == 200, response.text
    event_payload = {"node": "load-node", "actor": ACTOR, "machine": machine, "host": "load-node",
                     "host_cpu_load_per_cpu": 0.75}
    audit_payload = {"machine": machine, "host": "load-node", "host_cpu_load_per_cpu": 0.75}
    assert_event_and_audit(ctx, "node.load_limits_saved", aggregate="node", aggregate_id="load-node",
                           event_payload=event_payload, target="machine-load", action="limits", ticket="load-node",
                           actor=ACTOR, result="load limits saved", audit_payload=audit_payload)
