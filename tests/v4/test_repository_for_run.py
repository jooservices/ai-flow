"""Characterize run repository selection before making it available to routers."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aiflow.v4.control.app_helpers import install as install_app_helpers, repository_for_run
from aiflow.v4.control.features.projects.routes import _ticket_epic
from aiflow.v4.control.ticket_helpers import ticket_epic


class RecordingProject:
    def __init__(self, repo: str = "org/default"):
        self.repo = repo
        self.tickets: list[dict] = []

    def repo_for(self, ticket: dict) -> str:
        self.tickets.append(ticket)
        return f"{self.repo}:{ticket.get('epic', '')}"


class RecordingTickets:
    def __init__(self, ticket: dict | None = None, ancestors: dict[str, dict | None] | None = None):
        self.ticket = ticket
        self.ancestors = ancestors or {}
        self.lookups: list[str] = []

    def get_ticket_type_and_parent(self, params: tuple[str]) -> dict | None:
        self.lookups.append(params[0])
        return self.ticket

    def ticket_ancestor(self, key: str) -> dict | None:
        self.lookups.append(key)
        return self.ancestors.get(key)


def _legacy_helpers(ctx):
    def ticket_epic(ticket: dict) -> str:
        return _ticket_epic(SimpleNamespace(ctx=ctx), ticket)

    deps = SimpleNamespace(late=SimpleNamespace(projects_routes=SimpleNamespace(ticket_epic=ticket_epic)))
    return install_app_helpers(None, ctx, deps)


def _context(projects: dict[str, RecordingProject] | None = None, ticket: dict | None = None,
             ancestors: dict[str, dict | None] | None = None):
    tickets = RecordingTickets(ticket, ancestors)
    repositories = SimpleNamespace(projects=projects or {})
    ctx = SimpleNamespace(ticket_repository=tickets, repo=lambda: repositories)
    return ctx, tickets


def test_saved_repository_precedes_project_and_ticket_lookups():
    project = RecordingProject()
    ctx, tickets = _context({"DEMO": project}, {"key": "DEMO-1", "type": "Story"})

    repository = _legacy_helpers(ctx).repository_for_run(
        {"project": "DEMO", "ticket_key": "DEMO-1", "details": {"repository": "saved/repo"}})

    assert repository == "saved/repo"
    assert tickets.lookups == []
    assert project.tickets == []


def test_supplied_project_and_looked_up_project_keep_the_legacy_ticket_fallback():
    for supplied in (True, False):
        project = RecordingProject("org/project")
        ctx, tickets = _context({"DEMO": project}, ticket=None)
        selected = project if supplied else None

        repository = _legacy_helpers(ctx).repository_for_run(
            {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, selected)

        assert repository == "org/project:"
        assert project.tickets == [{"epic": ""}]
        assert tickets.lookups == ["DEMO-1"]


def test_missing_project_returns_empty_without_ticket_lookup():
    ctx, tickets = _context()

    repository = _legacy_helpers(ctx).repository_for_run(
        {"project": "MISSING", "ticket_key": "DEMO-1", "details": {}})

    assert repository == ""
    assert tickets.lookups == []


def test_ticket_epic_resolves_ancestor_chain_and_missing_parent_fallback():
    tickets = RecordingTickets(ancestors={
        "DEMO-2": {"type": "Story", "parent_key": "DEMO-EPIC"},
        "DEMO-EPIC": {"type": "Epic", "parent_key": ""},
    })
    ctx = SimpleNamespace(ticket_repository=tickets)

    assert _ticket_epic(SimpleNamespace(ctx=ctx), {"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2"}) == "DEMO-EPIC"
    assert _ticket_epic(SimpleNamespace(ctx=ctx), {"key": "DEMO-3", "type": "Story", "parent_key": "UNKNOWN"}) == "UNKNOWN"
    assert tickets.lookups == ["DEMO-2", "DEMO-EPIC", "UNKNOWN"]


def test_module_helper_accepts_context_and_external_router_reads_it_from_app_state():
    project = RecordingProject("org/router")
    ctx, _tickets = _context({"DEMO": project}, {"key": "DEMO-1", "type": "Story"})
    app = FastAPI()
    app.state.ctx = ctx

    @app.get("/repository")
    def repository(request: Request):
        run = {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}
        return {"repository": repository_for_run(request.app.state.ctx, run)}

    response = TestClient(app).get("/repository")

    assert response.status_code == 200
    assert response.json() == {"repository": "org/router:"}


def test_module_helper_returns_saved_repository_and_uses_supplied_project():
    project = RecordingProject("org/supplied")
    ctx, tickets = _context(ticket={"key": "DEMO-1", "type": "Story"})

    assert repository_for_run(ctx, {"details": {"repository": "saved/repo"}}, project) == "saved/repo"
    assert repository_for_run(ctx, {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, project) == "org/supplied:"
    assert project.tickets == [{"key": "DEMO-1", "type": "Story", "epic": ""}]
    assert tickets.lookups == ["DEMO-1"]


def test_ticket_epic_handles_epic_ticket_and_cyclic_parent_chain():
    tickets = RecordingTickets(ancestors={
        "DEMO-2": {"type": "Story", "parent_key": "DEMO-1"},
        "DEMO-1": {"type": "Story", "parent_key": "DEMO-2"},
    })
    ctx = SimpleNamespace(ticket_repository=tickets)

    assert ticket_epic(ctx, {"key": "DEMO-EPIC", "type": "Epic"}) == "DEMO-EPIC"
    assert ticket_epic(ctx, {"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2"}) == ""
    assert tickets.lookups == ["DEMO-2", "DEMO-1"]
