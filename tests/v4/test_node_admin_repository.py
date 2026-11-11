"""Characterization and repository tests for the node administration active-node lookup."""
from __future__ import annotations

from dataclasses import replace

import pytest

from aiflow.v4.control.application_context import ApplicationContext
from aiflow.v4.control.features.fleet.repository import FakeNodeRepository, NodeRepository
from conftest import join_node
from test_node_operations import sign_in


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
        client, ctx, method, path, body, role, expected_status):
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
    sign_in(client, ctx)

    response = client.request("DELETE", f"/v1/nodes/{node}", json={"confirm": True})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "machine has active sessions; wait for them to finish before removing it"}
    assert db.one("SELECT removed_at FROM nodes WHERE name=%s", (node,)) == {"removed_at": None}
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE aggregate_id=%s", (node,)) == {"n": 0}
