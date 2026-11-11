"""Characterization and repository tests for the node administration active-node lookup."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

from aiflow.v4.control.application_context import ApplicationContext
from aiflow.v4.control.features.fleet import routes as fleet_routes
from aiflow.v4.control.features.fleet.repository import FakeNodeRepository, NodeRepository
from conftest import join_node
from test_node_operations import sign_in


def _assert_refused_node_audit(db, node, action, actor, result):
    assert db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit"
                  " WHERE target='node' AND action=%s AND ticket=%s", (action, node)) == [
        {"target": "node", "action": action, "ticket": node, "run_id": "", "fence": None,
         "actor": actor, "accepted": False, "result": result, "payload": {}}]


def _seed_node_cleanup_records(db, node):
    account_id = f"{node}-managed-account"
    token_hash = f"{node}-unused-token"
    db.run("INSERT INTO managed_ai_accounts (id,provider,node,sign_in_url,sign_in_code)"
           " VALUES (%s,'claude',%s,'https://auth.example.test/device','ABCD-EFGH')", (account_id, node))
    db.run("INSERT INTO accounts (id,provider,node,status) VALUES (%s,'claude',%s,'idle')", (account_id, node))
    db.run("INSERT INTO node_join_tokens (token_hash,node_name,role,project,expires_at)"
           " VALUES (%s,%s,'worker','TESTWP',now()+interval '1 day')", (token_hash, node))
    return account_id, token_hash


def _assert_node_cleanup_records_preserved(db, node, account_id, token_hash):
    assert db.one("SELECT removed_at IS NULL AS active,sign_in_url,sign_in_code FROM managed_ai_accounts"
                  " WHERE id=%s", (account_id,)) == {
        "active": True, "sign_in_url": "https://auth.example.test/device", "sign_in_code": "ABCD-EFGH"}
    assert db.one("SELECT node,status FROM accounts WHERE id=%s", (account_id,)) == {
        "node": node, "status": "idle"}
    assert db.one("SELECT used_at FROM node_join_tokens WHERE token_hash=%s", (token_hash,)) == {"used_at": None}


def _trace_concurrent_node_removal(ctx, db, monkeypatch, node):
    trace = []
    route_writes = []
    original_one = db.one
    original_run = db.run
    original_transaction = db.transaction
    original_emit = ctx.events.emit
    original_publish = fleet_routes.publish_for
    original_lookup = ctx.node_repository.active_node_exists

    def track_one(sql, params=None):
        if "SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL" in sql:
            trace.append("lookup")
        if "SELECT count(*) AS n FROM runs WHERE node=%s AND status='running'" in sql:
            trace.append("running_sessions")
        return original_one(sql, params)

    def track_run(sql, params=None):
        route_writes.append((sql, params))
        if sql.lstrip().startswith("UPDATE nodes SET accepting_work=%s"):
            trace.append("node_update")
        return original_run(sql, params)

    def remove_after_lookup(name):
        assert original_lookup(name)
        trace.append("concurrent_removal")
        original_run("UPDATE nodes SET removed_at=now(),accepting_work=false,token_hash='',status='offline'"
                     " WHERE name=%s", (name,))
        return True

    @contextmanager
    def track_transaction():
        with original_transaction() as connection:
            class ConnectionProxy:
                def execute(self, sql, params=None):
                    route_writes.append((sql, params))
                    if sql.lstrip().startswith("UPDATE nodes SET removed_at=now()"):
                        trace.append("node_update")
                    return connection.execute(sql, params)

            yield ConnectionProxy()

    def track_publish(*args, **kwargs):
        result = original_publish(*args, **kwargs)
        trace.append("domain_event")
        return result

    def track_emit(*args, **kwargs):
        trace.append("telemetry")
        return original_emit(*args, **kwargs)

    monkeypatch.setattr(db, "one", track_one)
    monkeypatch.setattr(db, "run", track_run)
    monkeypatch.setattr(db, "transaction", track_transaction)
    monkeypatch.setattr(ctx.node_repository, "active_node_exists", remove_after_lookup)
    monkeypatch.setattr(fleet_routes, "publish_for", track_publish)
    monkeypatch.setattr(ctx.events, "emit", track_emit)
    return trace, route_writes


def _assert_concurrent_node_effects(method, response, db, node, trace, route_writes):
    assert response.status_code == 200
    race_index = trace.index("concurrent_removal")
    assert trace.index("lookup") < race_index
    later_trace = trace[race_index + 1:]
    if method == "DELETE":
        assert later_trace.index("running_sessions") < later_trace.index("node_update")
        node_updates = [(sql, params) for sql, params in route_writes
                        if sql.lstrip().startswith("UPDATE nodes SET removed_at=now()")]
        assert len(node_updates) == 1, route_writes
        assert "WHERE name=%s" in node_updates[0][0]
        assert "removed_at IS NULL" not in node_updates[0][0]
        assert node_updates[0][1] == (node,)
        assert response.json() == {"node": node, "removed": True}
        assert db.one("SELECT removed_at IS NOT NULL AS removed FROM nodes WHERE name=%s", (node,)) == {
            "removed": True}
        assert db.one("SELECT type,aggregate,aggregate_id,payload FROM domain_events WHERE type='node.managed_removed'"
                      " AND aggregate_id=%s", (node,)) == {
            "type": "node.managed_removed", "aggregate": "node", "aggregate_id": node,
            "payload": {"node": node, "actor": "owner@example.test"}}
        assert db.one("SELECT type,outcome,reason FROM events WHERE type='node.removed' AND node=%s", (node,)) == {
            "type": "node.removed", "outcome": "removed", "reason": "removed from Machines and AI screen"}
    else:
        node_updates = [(sql, params) for sql, params in route_writes
                        if sql.lstrip().startswith("UPDATE nodes SET") and "accepting_work=%s" in sql]
        assert len(node_updates) == 1, route_writes
        assert "WHERE name=%s" in node_updates[0][0]
        assert "removed_at IS NULL" not in node_updates[0][0]
        assert node_updates[0][1] == (True, True, node)
        assert response.json() == {"node": node, "accepting_work": True}
        assert db.one("SELECT accepting_work,removed_at IS NOT NULL AS removed FROM nodes WHERE name=%s",
                      (node,)) == {"accepting_work": True, "removed": True}
        assert db.one("SELECT type,aggregate,aggregate_id,payload FROM domain_events"
                      " WHERE type='node.work_acceptance_changed' AND aggregate_id=%s", (node,)) == {
            "type": "node.work_acceptance_changed", "aggregate": "node", "aggregate_id": node,
            "payload": {"node": node, "actor": "owner@example.test", "accepting_work": True}}
        assert db.one("SELECT type,outcome,reason FROM events WHERE type='node.work_acceptance_changed'"
                      " AND node=%s", (node,)) == {
            "type": "node.work_acceptance_changed", "outcome": "resume_work", "reason": "accepting new work"}
    assert later_trace.index("node_update") < later_trace.index("domain_event") < later_trace.index("telemetry")
    assert db.one("SELECT accepted FROM write_audit WHERE target='node' AND ticket=%s ORDER BY id DESC LIMIT 1",
                  (node,)) == {"accepted": True}


def test_work_acceptance_keeps_the_existing_resume_and_stop_behavior(client, ctx, db):
    node = "jaif-302-work-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)

    resumed = client.put(f"/v1/nodes/{node}/work-acceptance?accepting_work=true", json={"confirm": False})
    stopped = client.put(f"/v1/nodes/{node}/work-acceptance?accepting_work=false", json={"confirm": True})

    assert resumed.status_code == stopped.status_code == 200
    assert resumed.json() == {"node": node, "accepting_work": True}
    assert stopped.json() == {"node": node, "accepting_work": False}
    assert db.one("SELECT accepting_work FROM nodes WHERE name=%s", (node,)) == {"accepting_work": False}
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s"
                  " AND type='node.work_acceptance_changed'", (node,)) == {"n": 2}
    assert db.all("SELECT action,accepted,result,payload FROM write_audit WHERE target='node' AND ticket=%s"
                  " AND action IN ('resume_work','stop_taking_work')"
                  " ORDER BY id", (node,)) == [
        {"action": "resume_work", "accepted": True, "result": "accepting new work", "payload": {}},
        {"action": "stop_taking_work", "accepted": True, "result": "stopping new work", "payload": {}}]
    assert db.all("SELECT type,node,outcome,reason FROM events WHERE node=%s"
                  " AND type='node.work_acceptance_changed' ORDER BY id", (node,)) == [
        {"type": "node.work_acceptance_changed", "node": node, "outcome": "resume_work",
         "reason": "accepting new work"},
        {"type": "node.work_acceptance_changed", "node": node, "outcome": "stop_taking_work",
         "reason": "new work disabled"}]


def test_confirmed_removal_keeps_the_existing_success_behavior(client, ctx, db):
    node = "jaif-302-remove-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)

    response = client.request("DELETE", f"/v1/nodes/{node}", json={"confirm": True})

    assert response.status_code == 200
    assert response.json() == {"node": node, "removed": True}
    assert db.one("SELECT removed_at IS NOT NULL AS removed FROM nodes WHERE name=%s", (node,)) == {
        "removed": True}
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s"
                  " AND type='node.managed_removed'", (node,)) == {"n": 1}
    assert db.one("SELECT target,action,ticket,actor,accepted,result,payload FROM write_audit"
                  " WHERE target='node' AND action='remove' AND ticket=%s", (node,)) == {
        "target": "node", "action": "remove", "ticket": node, "actor": "owner@example.test",
        "accepted": True, "result": "machine removed", "payload": {}}
    assert db.one("SELECT type,node,outcome,reason FROM events WHERE type='node.removed' AND node=%s", (node,)) == {
        "type": "node.removed", "node": node, "outcome": "removed",
        "reason": "removed from Machines and AI screen"}

    repeated = client.request("DELETE", f"/v1/nodes/{node}", json={"confirm": True})

    assert repeated.status_code == 404
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s"
                  " AND type='node.managed_removed'", (node,)) == {"n": 1}


def test_stop_and_removal_without_confirmation_are_refused_before_the_node_lookup(
        client, ctx, db, monkeypatch):
    node = "jaif-302-unconfirmed-node"
    assert join_node(client, node).status_code == 200
    sign_in(client, ctx)
    original_one = db.one
    lookups = []

    def track_active_node_query(sql, params=None):
        if "SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL" in sql:
            lookups.append(tuple(params or ()))
        return original_one(sql, params)

    monkeypatch.setattr(db, "one", track_active_node_query)
    stopped = client.put(f"/v1/nodes/{node}/work-acceptance?accepting_work=false", json={})
    removed = client.request("DELETE", f"/v1/nodes/{node}", json={})

    assert stopped.status_code == 400
    assert removed.status_code == 400
    assert lookups == []
    assert db.one("SELECT accepting_work,removed_at FROM nodes WHERE name=%s", (node,)) == {
        "accepting_work": True, "removed_at": None}
    _assert_refused_node_audit(db, node, "stop_taking_work", "owner@example.test", "confirmation required")
    _assert_refused_node_audit(db, node, "remove", "owner@example.test", "confirmation required")


def test_node_repository_checks_active_names_as_literal_parameters(db):
    active_names = ("", "n" * 512, "节点 ' OR true; --")
    for name in active_names:
        db.run("INSERT INTO nodes (name,status) VALUES (%s,%s)", (name, "draining"))
    removed_name = "previously-removed-node"
    db.run("INSERT INTO nodes (name,removed_at) VALUES (%s,now())", (removed_name,))
    repository = NodeRepository(db)

    assert all(repository.active_node_exists(name) for name in active_names)
    assert not repository.active_node_exists(removed_name)
    assert not repository.active_node_exists("missing ' OR true --")


def test_fake_node_repository_models_only_caller_supplied_active_names():
    names = {"", "node'; DROP TABLE nodes; --", "unicode-東京"}
    repository = FakeNodeRepository(names)
    separate_repository = FakeNodeRepository()

    assert repository.active_node_exists("") is True
    assert repository.active_node_exists("node'; DROP TABLE nodes; --") is True
    assert repository.active_node_exists("unicode-東京") is True
    assert repository.active_node_exists("missing") is False
    assert repository.calls == ["", "node'; DROP TABLE nodes; --", "unicode-東京", "missing"]
    assert separate_repository.calls == []


def test_context_defaults_node_repository_and_preserves_injected_fake_and_positional_arguments(ctx):
    fake = FakeNodeRepository({"injected-node"})
    legacy = ApplicationContext(ctx.settings, ctx.db, ctx.leases, ctx.events, ctx.repo, ctx.jira)
    injected = replace(ctx, node_repository=fake)

    assert isinstance(ctx.node_repository, NodeRepository)
    assert isinstance(legacy.node_repository, NodeRepository)
    assert legacy.jira is ctx.jira
    assert injected.node_repository is fake
    assert injected.node_repository.active_node_exists("injected-node")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/v1/nodes/{name}/work-acceptance?accepting_work=false", {"confirm": True}),
        ("DELETE", "/v1/nodes/{name}", {"confirm": True}),
    ],
)
def test_routes_use_the_injected_fake_even_when_the_database_has_the_node(client, ctx, db, method, path, body):
    node = "jaif-302-fake-missing-node"
    db.run("INSERT INTO nodes (name) VALUES (%s)", (node,))
    sign_in(client, ctx)
    fake = FakeNodeRepository()
    ctx.node_repository = fake

    response = client.request(method, path.format(name=node), json=body)

    assert response.status_code == 404
    assert response.json() == {"detail": "machine is not registered"}
    assert fake.calls == [node]
    assert db.one("SELECT accepting_work,removed_at FROM nodes WHERE name=%s", (node,)) == {
        "accepting_work": True, "removed_at": None}
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s", (node,)) == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM events WHERE type IN (%s,%s)",
                  ("node.work_acceptance_changed", "node.removed")) == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE ticket=%s AND accepted=true", (node,)) == {"n": 0}


@pytest.mark.parametrize(
    ("method", "path", "body", "role", "expected_status"),
    [
        ("PUT", "/v1/nodes/{name}/work-acceptance?accepting_work=false", {"confirm": True}, None, 401),
        ("DELETE", "/v1/nodes/{name}", {"confirm": True}, None, 401),
        ("PUT", "/v1/nodes/{name}/work-acceptance?accepting_work=false", {"confirm": True}, "reviewer", 403),
        ("DELETE", "/v1/nodes/{name}", {"confirm": True}, "reviewer", 403),
    ],
)
def test_authorization_refuses_both_routes_before_repository_lookup(
        client, ctx, db, method, path, body, role, expected_status):
    node = "jaif-302-unauthorized-node"
    fake = FakeNodeRepository({node})
    ctx.node_repository = fake
    if role:
        sign_in(client, ctx, role=role)
    else:
        sign_in(client, ctx)
        client.cookies.clear()

    response = client.request(method, path.format(name=node), json=body)

    assert response.status_code == expected_status
    assert fake.calls == []
    action = "stop_taking_work" if method == "PUT" else "remove"
    actor = "owner@example.test" if role else ""
    result = "not available for your role" if role else "authenticated console session required"
    _assert_refused_node_audit(db, node, action, actor, result)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/v1/nodes/{name}/work-acceptance?accepting_work=false", {"confirm": True}),
        ("DELETE", "/v1/nodes/{name}", {"confirm": True}),
    ],
)
def test_lookup_error_propagates_without_success_side_effects(
        client, ctx, db, monkeypatch, method, path, body):
    node = "jaif-302-lookup-error-node"
    db.run("INSERT INTO nodes (name) VALUES (%s)", (node,))
    sign_in(client, ctx)
    original_one = db.one

    def fail_active_lookup(sql, params=None):
        if "SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL" in sql:
            raise RuntimeError("active node query failed")
        return original_one(sql, params)

    monkeypatch.setattr(db, "one", fail_active_lookup)
    with pytest.raises(RuntimeError, match="active node query failed"):
        client.request(method, path.format(name=node), json=body)

    monkeypatch.setattr(db, "one", original_one)
    assert db.one("SELECT accepting_work,removed_at FROM nodes WHERE name=%s", (node,)) == {
        "accepting_work": True, "removed_at": None}
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s", (node,)) == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM events WHERE type IN (%s,%s)",
                  ("node.work_acceptance_changed", "node.removed")) == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE ticket=%s AND accepted=true", (node,)) == {"n": 0}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/v1/nodes/{name}/work-acceptance?accepting_work=false", {"confirm": True}),
        ("DELETE", "/v1/nodes/{name}", {"confirm": True}),
    ],
)
def test_removed_node_returns_existing_not_registered_response(client, ctx, db, method, path, body):
    node = "jaif-302-soft-removed-node"
    db.run("INSERT INTO nodes (name,removed_at) VALUES (%s,now())", (node,))
    sign_in(client, ctx)

    response = client.request(method, path.format(name=node), json=body)

    assert response.status_code == 404
    assert response.json() == {"detail": "machine is not registered"}


def test_running_session_blocks_removal_without_cleanup(client, ctx, db):
    node = "jaif-302-running-node"
    db.run("INSERT INTO nodes (name) VALUES (%s)", (node,))
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence)"
           " VALUES ('4b17a1d2-4fa2-42f0-97c0-c14a6f6801b5','JAIF-302','TESTWP','dev','account',%s,1)",
           (node,))
    account_id, token_hash = _seed_node_cleanup_records(db, node)
    sign_in(client, ctx)

    response = client.request("DELETE", f"/v1/nodes/{node}", json={"confirm": True})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "machine has active sessions; wait for them to finish before removing it"}
    assert db.one("SELECT removed_at FROM nodes WHERE name=%s", (node,)) == {"removed_at": None}
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s", (node,)) == {"n": 0}
    _assert_node_cleanup_records_preserved(db, node, account_id, token_hash)
    assert db.one("SELECT count(*) AS n FROM events WHERE node=%s AND type='node.removed'", (node,)) == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='node' AND ticket=%s AND accepted", (node,)) == {
        "n": 0}


def test_node_removal_rolls_back_cleanup_and_success_side_effects_after_publish_failure(
        client, ctx, db, monkeypatch):
    node = "jaif-302-removal-rollback-node"
    db.run("INSERT INTO nodes (name) VALUES (%s)", (node,))
    account_id, token_hash = _seed_node_cleanup_records(db, node)
    sign_in(client, ctx)
    original_publish = fleet_routes.publish_for

    def publish_then_fail(*args, **kwargs):
        original_publish(*args, **kwargs)
        raise RuntimeError("forced publish failure")

    monkeypatch.setattr(fleet_routes, "publish_for", publish_then_fail)
    with pytest.raises(RuntimeError, match="forced publish failure"):
        client.request("DELETE", f"/v1/nodes/{node}", json={"confirm": True})

    assert db.one("SELECT removed_at IS NULL AS active,accepting_work,token_hash,status FROM nodes"
                  " WHERE name=%s", (node,)) == {
        "active": True, "accepting_work": True, "token_hash": "", "status": "joined"}
    _assert_node_cleanup_records_preserved(db, node, account_id, token_hash)
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s", (node,)) == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM events WHERE node=%s AND type='node.removed'", (node,)) == {"n": 0}
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='node' AND ticket=%s AND accepted", (node,)) == {
        "n": 0}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/v1/nodes/{name}/work-acceptance?accepting_work=true", {"confirm": False}),
        ("DELETE", "/v1/nodes/{name}", {"confirm": True}),
    ],
)
def test_concurrent_removal_after_precheck_keeps_original_write_order_and_effects(
        client, ctx, db, monkeypatch, method, path, body):
    node = "jaif-302-concurrent-removal-node"
    db.run("INSERT INTO nodes (name) VALUES (%s)", (node,))
    sign_in(client, ctx)
    trace, route_writes = _trace_concurrent_node_removal(ctx, db, monkeypatch, node)

    response = client.request(method, path.format(name=node), json=body)

    _assert_concurrent_node_effects(method, response, db, node, trace, route_writes)


def test_repeated_work_acceptance_request_keeps_each_audit_event_and_telemetry_row(client, ctx, db):
    node = "jaif-302-repeated-work-node"
    db.run("INSERT INTO nodes (name) VALUES (%s)", (node,))
    sign_in(client, ctx)

    first = client.put(f"/v1/nodes/{node}/work-acceptance?accepting_work=false", json={"confirm": True})
    second = client.put(f"/v1/nodes/{node}/work-acceptance?accepting_work=false", json={"confirm": True})

    assert first.status_code == second.status_code == 200
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s"
                  " AND type='node.work_acceptance_changed'", (node,)) == {"n": 2}
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='node' AND action='stop_taking_work'"
                  " AND ticket=%s AND accepted", (node,)) == {"n": 2}
    assert db.one("SELECT count(*) AS n FROM events WHERE node=%s AND type='node.work_acceptance_changed'",
                  (node,)) == {"n": 2}
