"""The tickets feature repository returns typed entities (JAIF-470)."""
from aiflow.v4.control.features.tickets.models import Ticket
from aiflow.v4.control.features.tickets.repository import TicketRepository

from tests.v4.conftest import add_ticket


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
