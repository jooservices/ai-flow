"""Exercise the context-based repository selection API exposed to routers."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aiflow.v4.control.app_helpers import repository_for_run


class RecordingProject:
    def __init__(self, repo: str):
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


def _context(project: RecordingProject, ticket: dict, ancestors: dict[str, dict | None]):
    tickets = RecordingTickets(ticket, ancestors)
    ctx = SimpleNamespace(
        ticket_repository=tickets,
        repo=lambda: SimpleNamespace(projects={"DEMO": project}),
    )
    return ctx, tickets


def test_module_helper_accepts_context_and_external_router_resolves_epic():
    ticket = {"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2"}
    project = RecordingProject("org/router")
    ctx, tickets = _context(
        project,
        ticket,
        {
            "DEMO-2": {"type": "Story", "parent_key": "DEMO-EPIC"},
            "DEMO-EPIC": {"type": "Epic", "parent_key": ""},
        },
    )
    app = FastAPI()
    app.state.ctx = ctx

    @app.get("/repository")
    def repository(request: Request):
        run = {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}
        return {"repository": repository_for_run(request.app.state.ctx, run)}

    response = TestClient(app).get("/repository")

    assert response.status_code == 200
    assert response.json() == {"repository": "org/router:DEMO-EPIC"}
    assert project.tickets == [{**ticket, "epic": "DEMO-EPIC"}]
    assert tickets.lookups == ["DEMO-1", "DEMO-2", "DEMO-EPIC"]


def test_module_helper_returns_saved_repository_and_uses_supplied_project():
    project = RecordingProject("org/supplied")
    ctx, tickets = _context(project, {"key": "DEMO-1", "type": "Story"}, {})

    assert repository_for_run(ctx, {"details": {"repository": "saved/repo"}}, project) == "saved/repo"
    assert repository_for_run(
        ctx, {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, project
    ) == "org/supplied:"
    assert project.tickets == [{"key": "DEMO-1", "type": "Story", "epic": ""}]
    assert tickets.lookups == ["DEMO-1"]
