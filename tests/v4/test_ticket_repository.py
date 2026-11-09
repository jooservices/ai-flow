"""JAIF-299: the ticket repository owns ticket queries and supports an application-context fake."""
import ast
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control import api
from aiflow.v4.control.features.tickets.models import Ticket, TicketMove
from aiflow.v4.control.features.tickets.repository import FakeTicketRepository, TicketRepository, ticket_repository_of

from tests.v4.conftest import add_ticket


def test_ticket_repository_accessor_reuses_shared_repository_or_adapts_legacy_context():
    shared = FakeTicketRepository()
    shared_context = SimpleNamespace(db=object(), ticket_repository=shared)
    assert ticket_repository_of(shared_context) is shared

    database = object()
    legacy_context = SimpleNamespace(db=database)
    adapted = ticket_repository_of(legacy_context)
    assert isinstance(adapted, TicketRepository) and adapted.db is database
    assert ticket_repository_of(legacy_context, fallback=False) is None


def test_get_returns_a_typed_frozen_ticket_or_none(db):
    add_ticket(db, "TESTWP-1", status="To Do", labels=("aiflow-ready", "x"), blocked_by=("TESTWP-0",))
    repository = TicketRepository(db)

    ticket = repository.get("TESTWP-1")

    assert ticket == Ticket(key="TESTWP-1", project="TESTWP", status="To Do", type="Task", priority="Medium",
                            summary="Summary of TESTWP-1", description="Acceptance criteria:\n- AC1: works",
                            labels=("aiflow-ready", "x"), parent_key=None, blocked_by=("TESTWP-0",))
    assert repository.get("TESTWP-404") is None


def test_updates_change_status_labels_and_description(db):
    add_ticket(db, "TESTWP-2", labels=("needs-owner", "keep"))
    repository = TicketRepository(db)

    repository.update_status("TESTWP-2", "In Progress")
    repository.remove_label("TESTWP-2", "needs-owner")
    repository.update_description("TESTWP-2", "new text")

    ticket = repository.get("TESTWP-2")
    assert (ticket.status, ticket.labels, ticket.description) == ("In Progress", ("keep",), "new text")


def test_fake_repository_matches_ticket_reads_and_updates():
    repository = FakeTicketRepository([{
        "key": "TESTWP-3", "project": "TESTWP", "status": "To Do", "type": "Task", "priority": "Medium",
        "summary": "Summary", "description": "Acceptance", "labels": ["remove", "keep"], "parent_key": None,
        "blocked_by": [],
    }])

    ticket = repository.get("TESTWP-3")
    assert ticket == Ticket(key="TESTWP-3", project="TESTWP", status="To Do", type="Task", priority="Medium",
                            summary="Summary", description="Acceptance", labels=("remove", "keep"),
                            parent_key=None, blocked_by=())
    repository.update_status("TESTWP-3", "In Progress")
    repository.remove_label("TESTWP-3", "remove")
    repository.update_description("TESTWP-3", "new text")

    updated = repository.get("TESTWP-3")
    assert (updated.status, updated.labels, updated.description) == ("In Progress", ("keep",), "new text")
    assert repository.get("TESTWP-404") is None


QUERY_ARGUMENTS = {
    "get": ("TESTWP-1",),
    "update_status": ("TESTWP-1", "To Do"),
    "remove_label": ("TESTWP-1", "label"),
    "update_description": ("TESTWP-1", "text"),
    "epics": ("TESTWP",),
    "open_project_tickets": ("TESTWP", "TESTWP-0"),
    "change_requests": ("TESTWP", "change-request"),
    "epic_in_project": ("TESTWP-1", "TESTWP"),
    "review_metric_tickets": (None, "", ()),
    "review_queue_candidates": ("", None),
    "hotfile_past_files": ("TESTWP-1", "TESTWP-2", 8),
    "hotfile_epic": ("TESTWP-1",),
    "hotfile_in_flight": ("",),
    "cleanup_hot_file_reservations": (None, 120),
    "knowledge_ticket_ancestor": ("TESTWP-1",),
    "knowledge_capture_run": ("run-1",),
    "memory_index_tickets": (20_000,),
    "memory_index_run_summaries": (20_000,),
    "memory_index_findings": (20_000,),
    "disagreements": (300,),
    "scale_history_tickets": (None, []),
    "scale_wave_tickets": (None, []),
    "scale_mark_done": ("TESTWP-1",),
    "scale_restore_todo": ("TESTWP-1",),
    "list_project_tickets_for_epic_reconcile": ("TESTWP",),
    "latest_project_ticket_moves": ("TESTWP",),
    "verify_candidates": ([],),
    "triage_candidates": ([],),
    "intake_candidates": ([],),
    "impact_candidates": ([],),
    "developer_candidates": ([],),
    "finished_without_person": (None, None, None),
    "needed_a_person": (None, None, None),
    "repeated_failure_events": (None, None, None),
    "current_repeated_failures": (None, None),
    "capacity_ready_tickets": ((True, []),),
    "capacity_decisions": ((True, []),),
    "capacity_estimate_rows": ((True, []),),
    "version_links": ([],),
    "progress_tickets": ([],),
    "readiness_tickets": ([],),
    "blocker_tickets": ([],),
    "timeline_ticket": ("TESTWP-1",),
    "ticket_ancestor": ("TESTWP-1",),
    "ticket_detail": ("TESTWP-1",),
}


@pytest.mark.parametrize("name", TicketRepository.NAMED_QUERIES)
def test_fake_repository_matches_every_named_ticket_query(name):
    ticket_row = {"key": "TESTWP-1", "project": "TESTWP", "status": "To Do"}
    move_row = {"ticket": "TESTWP-1", "ts": datetime.now(timezone.utc)}

    class RecordingDatabase:
        def __init__(self):
            self.calls = []

        def one(self, sql, params=()):
            self.calls.append(("one", sql, params))
            return {"key": "TESTWP-1", "project": "TESTWP", "status": "To Do"}

        def all(self, sql, params=()):
            self.calls.append(("all", sql, params))
            if "WITH RECURSIVE epic_tree" in sql:
                return [ticket_row]
            if "SELECT t.key AS ticket, latest_move.ts" in sql:
                return [move_row]
            return [{"query": "all"}]

        def run(self, sql, params=()):
            self.calls.append(("run", sql, params))
            return 1

    class RecordingExecutor:
        def execute(self, sql, params=()):
            return None

        def executemany(self, sql, rows):
            return None

    args = list(QUERY_ARGUMENTS.get(name, ()))
    if name in {"cleanup_hot_file_reservations", "scale_history_tickets", "scale_wave_tickets"}:
        args[0] = RecordingExecutor()
    database = RecordingDatabase()
    expected = getattr(TicketRepository(database), name)(*args)
    operation = database.calls[-1][0] if database.calls else "execute"
    response_by_operation = {"one": ticket_row, "all": [{"query": "all"}], "run": 1}
    if name == "list_project_tickets_for_epic_reconcile":
        response_by_operation["all"] = [ticket_row]
    elif name == "latest_project_ticket_moves":
        response_by_operation["all"] = [move_row]
    responses = {name: response_by_operation[operation]} if operation in response_by_operation else {}
    fake = FakeTicketRepository(tickets=[ticket_row], responses=responses)

    assert getattr(fake, name)(*args) == expected
    assert fake.calls[-1][0] == name


def test_epic_reconcile_repository_returns_frozen_tickets_and_moves(db):
    add_ticket(db, "TESTWP-710", status="In Progress")
    add_ticket(db, "OTHER-711", status="To Do")
    db.run("UPDATE tickets SET type='Epic' WHERE key=%s", ("TESTWP-710",))
    db.run("UPDATE tickets SET parent_key=%s WHERE key=%s", ("TESTWP-710", "OTHER-711"))
    db.run("INSERT INTO ticket_moves (ticket,from_status,to_status,cause) VALUES (%s,%s,%s,%s)",
           ("OTHER-711", "To Do", "In Progress", "person"))
    repository = TicketRepository(db)

    tickets = repository.list_project_tickets_for_epic_reconcile(("TESTWP",))
    moves = repository.latest_project_ticket_moves("OTHER")

    by_key = {ticket.key: ticket for ticket in tickets}
    assert isinstance(by_key["TESTWP-710"], Ticket)
    assert (by_key["TESTWP-710"].project, by_key["TESTWP-710"].status,
            by_key["TESTWP-710"].type) == ("TESTWP", "In Progress", "Epic")
    assert by_key["OTHER-711"].parent_key == "TESTWP-710"
    assert moves == [TicketMove(ticket="OTHER-711", ts=moves[0].ts)]


def test_route_uses_fake_repository_injected_through_application_context(ctx):
    fake = FakeTicketRepository(responses={
        "timeline_ticket": {"key": "TESTWP-901", "status": "To Do", "labels": [], "done_at": None},
    })
    injected = replace(ctx, ticket_repository=fake)
    app = api.create_app(injected)

    with TestClient(app) as client:
        response = client.get("/v1/tickets/TESTWP-901/timeline")

    assert response.status_code == 200
    assert fake.calls == [("timeline_ticket", ("TESTWP-901",))]


def test_control_callers_only_use_defined_ticket_repository_methods():
    control = Path(__file__).parents[2] / "aiflow" / "v4" / "control"
    methods = {name for name in dir(TicketRepository) if not name.startswith("__")}
    missing = []
    for path in control.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            value = node.value
            context_repository = (isinstance(value, ast.Attribute) and value.attr == "ticket_repository")
            accessor_repository = (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                                   and value.func.id == "ticket_repository_of")
            if (context_repository or accessor_repository) and node.attr not in methods:
                missing.append(f"{path}:{node.lineno}: {node.attr}")
    assert missing == []


def test_ticket_table_sql_is_owned_by_repository_modules():
    control = Path(__file__).parents[2] / "aiflow" / "v4" / "control"
    table_sql = re.compile(
        r"\b(?:FROM|JOIN|UPDATE|INTO|TRUNCATE)\s+"
        r"(?:tickets|ticket_versions|ticket_version_added|ticket_disagreements)\b",
        re.IGNORECASE,
    )
    violations = []
    for path in control.rglob("*.py"):
        if "migrations" in path.parts or "repositories" in path.parts or path.name == "repository.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and table_sql.search(node.value):
                violations.append(f"{path}:{node.lineno}")
    assert violations == []
