"""Characterize run repository selection before making it available to routers."""

from __future__ import annotations

from types import SimpleNamespace

from aiflow.v4.control.app_helpers import install as install_app_helpers
from aiflow.v4.control.features.projects.routes import _ticket_epic


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
    return install_app_helpers(None, ctx, SimpleNamespace())


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


def test_supplied_project_is_used_for_legacy_ticket_fallback():
    supplied = RecordingProject("org/supplied")
    looked_up = RecordingProject("org/looked-up")
    ctx, tickets = _context({"DEMO": looked_up}, ticket=None)

    repository = _legacy_helpers(ctx).repository_for_run(
        {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, supplied)

    assert repository == "org/supplied:"
    assert supplied.tickets == [{"epic": ""}]
    assert looked_up.tickets == []
    assert tickets.lookups == ["DEMO-1"]


def test_looked_up_project_keeps_the_legacy_ticket_fallback():
    project = RecordingProject("org/looked-up")
    ctx, tickets = _context({"DEMO": project}, ticket=None)

    repository = _legacy_helpers(ctx).repository_for_run(
        {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}})

    assert repository == "org/looked-up:"
    assert project.tickets == [{"epic": ""}]
    assert tickets.lookups == ["DEMO-1"]


def test_installed_helper_uses_resolved_epic_for_legacy_run_repository_selection():
    ticket = {"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2"}
    project = RecordingProject("org/project")
    ctx, tickets = _context(
        {"DEMO": project},
        ticket,
        {
            "DEMO-2": {"type": "Story", "parent_key": "DEMO-EPIC"},
            "DEMO-EPIC": {"type": "Epic", "parent_key": ""},
        },
    )

    repository = _legacy_helpers(ctx).repository_for_run(
        {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}})

    assert repository == "org/project:DEMO-EPIC"
    assert project.tickets == [{**ticket, "epic": "DEMO-EPIC"}]
    assert tickets.lookups == ["DEMO-1", "DEMO-2", "DEMO-EPIC"]


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


def test_ticket_epic_handles_epic_ticket_and_cyclic_parent_chain():
    tickets = RecordingTickets(ancestors={
        "DEMO-2": {"type": "Story", "parent_key": "DEMO-1"},
        "DEMO-1": {"type": "Story", "parent_key": "DEMO-2"},
    })
    ctx = SimpleNamespace(ticket_repository=tickets)

    assert _ticket_epic(SimpleNamespace(ctx=ctx), {"key": "DEMO-EPIC", "type": "Epic"}) == "DEMO-EPIC"
    assert _ticket_epic(SimpleNamespace(ctx=ctx), {"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2"}) == ""
    assert tickets.lookups == ["DEMO-2", "DEMO-1"]
