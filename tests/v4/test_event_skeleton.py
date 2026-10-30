"""The walking skeleton of the event platform (JAIF-468): `ticket.moved` end to end."""
import threading
import time

from aiflow.v4.control import merge_flow
from aiflow.v4.control.features.tickets.events import TicketMoved
from aiflow.v4.control.platform.events import wiring

from tests.v4.conftest import add_ticket


def test_a_move_publishes_ticket_moved_and_keeps_the_telemetry_row_in_the_same_transaction(ctx, db):
    add_ticket(db, "TESTWP-1")

    merge_flow.record_move(ctx, "TESTWP-1", "To Do", "In Progress", "worker", "dev started")

    domain = db.one("SELECT * FROM domain_events WHERE type='ticket.moved' AND aggregate_id='TESTWP-1'")
    assert domain["payload"] == {"ticket_key": "TESTWP-1", "from_status": "To Do", "to_status": "In Progress",
                                 "cause": "worker", "detail": "dev started"}
    telemetry = db.one("SELECT outcome, reason FROM events WHERE type='ticket.moved' AND ticket='TESTWP-1'")
    assert telemetry == {"outcome": "In Progress", "reason": "worker: dev started"}
    assert db.one("SELECT count(*) AS n FROM ticket_moves WHERE ticket='TESTWP-1'")["n"] == 1


def test_an_outbox_listener_receives_the_event_through_the_dispatcher_with_a_measured_delay(ctx, db):
    add_ticket(db, "TESTWP-2")
    seen = []
    wiring.registry_for(ctx).on(TicketMoved, name="test.collect")(lambda envelope, item: seen.append((envelope, item)))

    started = time.perf_counter()
    merge_flow.record_move(ctx, "TESTWP-2", "To Do", "Done", "merge", "")
    assert seen == []  # the listener never runs inside the publisher's call
    assert wiring.dispatcher_for(ctx).run_once() == 3  # one delivery per outbox listener of TicketMoved
    delay = time.perf_counter() - started

    envelope, item = seen[0]
    assert (item.ticket_key, item.to_status, envelope.aggregate) == ("TESTWP-2", "Done", "ticket")
    assert delay < 1.0  # one dispatcher pass after the move; the loop runs it every second
    assert db.one("SELECT status FROM domain_event_deliveries WHERE listener='test.collect'")["status"] == "done"


def test_the_dispatch_loop_delivers_until_it_is_stopped(ctx, db, monkeypatch):
    add_ticket(db, "TESTWP-3")
    monkeypatch.setattr(wiring, "DISPATCH_EVERY_S", 0.05)
    delivered = threading.Event()
    wiring.registry_for(ctx).on(TicketMoved, name="test.signal")(lambda envelope, item: delivered.set())
    stop = threading.Event()
    thread = threading.Thread(target=wiring.dispatch_loop, args=(ctx, stop), daemon=True)
    thread.start()
    try:
        merge_flow.record_move(ctx, "TESTWP-3", "To Do", "In Progress", "worker")
        assert delivered.wait(5)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
