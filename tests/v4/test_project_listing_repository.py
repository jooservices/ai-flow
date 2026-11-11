"""Data access and injection tests for the managed project listing."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiflow.v4.control import system_check_api, tooling
from aiflow.v4.control.application_context import ApplicationContext
from aiflow.v4.control.features.projects import repository, service as project_service
from aiflow.v4.core import pipeline_checks


class RecordingDB:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []

    def one(self, sql, params=None):
        self.statements.append((sql, params))
        return self.rows.pop(0)


class ListingFake:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls = 0

    def listing_data(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data


def _empty_row():
    return {"ticket_counts": {}, "node_rows": [], "probe_rows": []}


def test_project_listing_repository_preserves_aggregate_query_and_compatibility_entry_point():
    row = {"ticket_counts": {"TESTWP": 4}, "node_rows": [{"name": "node1", "readiness": None}],
           "probe_rows": [{"project": "TESTWP", "node": "node1", "results": []}]}
    db = RecordingDB([row, row])
    expected = repository.ProjectListingData(**row)

    assert repository.ProjectListingRepository(db).listing_data() == expected
    assert repository.listing_data(db) == expected

    assert len(db.statements) == 2
    sql, params = db.statements[0]
    assert params is None
    assert "jsonb_object_agg(project, ticket_count)" in sql
    assert "jsonb_agg(jsonb_build_object('name', name, 'readiness', readiness" in sql
    assert "jsonb_agg(jsonb_build_object('project', project, 'node', node" in sql
    assert "ORDER BY name" in sql and "ORDER BY node" in sql


def test_project_listing_repository_preserves_empty_aggregates():
    db = RecordingDB([_empty_row()])

    result = repository.ProjectListingRepository(db).listing_data()

    assert result == repository.ProjectListingData(ticket_counts={}, node_rows=[], probe_rows=[])
    assert len(db.statements) == 1


def test_application_context_owns_a_repository_per_database_and_retains_injected_fake(ctx):
    first = ctx.project_listing_repository
    second_db = object()
    second = replace(ctx, db=second_db)
    separately_constructed = ApplicationContext(settings=ctx.settings, db=second_db, leases=ctx.leases,
                                                events=ctx.events, repo=ctx.repo)
    fake = ListingFake()
    injected = ApplicationContext(settings=ctx.settings, db=second_db, leases=ctx.leases, events=ctx.events,
                                  repo=ctx.repo, project_listing_repository=fake)

    assert isinstance(first, repository.ProjectListingRepository)
    assert first.db is ctx.db
    assert isinstance(second.project_listing_repository, repository.ProjectListingRepository)
    assert second.project_listing_repository is not first
    assert second.project_listing_repository.db is second_db
    assert separately_constructed.project_listing_repository.db is second_db
    assert injected.project_listing_repository is fake


def test_managed_project_list_uses_fake_data_in_existing_processing(ctx, monkeypatch):
    data = repository.ProjectListingData(
        ticket_counts={"TESTWP": 73, "UNKNOWN": 99},
        node_rows=[{"name": "fake-node", "readiness": None}],
        probe_rows=[{"project": "TESTWP", "node": "fake-node", "results": []}],
    )
    fake = ListingFake(data=data)
    ctx.project_listing_repository = fake
    project = SimpleNamespace(key="TESTWP", repo="example/testwp", configured_repositories=[], paused=True,
                              priority=2, base="main", stack="python", required_services=[], checks=("pytest",),
                              local_gate="gate")
    repo = SimpleNamespace(projects={"TESTWP": project})
    readiness_calls = []
    probe_calls = []
    monkeypatch.setattr(system_check_api, "projects_readiness",
                        lambda _ctx, _repo, nodes: readiness_calls.append(nodes) or {"TESTWP": ["ready"]})
    monkeypatch.setattr(tooling, "probe_report",
                        lambda _ctx, repo, probe_rows: probe_calls.append((repo, probe_rows)) or {"TESTWP": {"ok": True}})
    monkeypatch.setattr(pipeline_checks, "project_catalog", lambda _gate: (["check"], ["pipeline"]))

    rows = project_service.managed_project_list(ctx, repo)

    assert fake.calls == 1
    assert readiness_calls == [data.node_rows]
    assert probe_calls == [(repo, data.probe_rows)]
    assert rows == [{"key": "TESTWP", "repo": "example/testwp", "repositories": [], "status": "paused",
                     "priority": 2, "ticket_count": 73, "base": "main", "stack": "python",
                     "node_readiness": ["ready"], "services": [], "checks": {"ok": True},
                     "check_commands": ["pytest"], "pipeline_checks": ["check"],
                     "pipeline_only": ["pipeline"]}]


def test_managed_project_list_propagates_repository_errors_without_writes(ctx, monkeypatch):
    error = RuntimeError("listing repository failed")
    fake = ListingFake(error=error)
    ctx.project_listing_repository = fake
    writes = []
    monkeypatch.setattr(ctx.db, "run", lambda *args, **kwargs: writes.append((args, kwargs)))

    with pytest.raises(RuntimeError) as raised:
        project_service.managed_project_list(ctx, SimpleNamespace(projects={}))

    assert raised.value is error
    assert fake.calls == 1
    assert writes == []


def test_assembled_projects_route_uses_injected_repository_data(client, ctx):
    data = repository.ProjectListingData(ticket_counts={"TESTWP": 81}, node_rows=[], probe_rows=[])
    fake = ListingFake(data=data)
    ctx.settings.console_login = False
    ctx.project_listing_repository = fake

    response = client.get("/v1/projects")

    assert response.status_code == 200
    listed = next(row for row in response.json()["projects"] if row["key"] == "TESTWP")
    assert listed["ticket_count"] == 81
    assert fake.calls == 1
