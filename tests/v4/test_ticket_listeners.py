"""The listeners of TicketMoved (JAIF-471): epic completion report and the human-review notice."""
from aiflow.v4.control import merge_flow, projects as project_lifecycle
from aiflow.v4.control.platform.events import wiring

from tests.v4.conftest import add_ticket


def _deliver(ctx):
    return wiring.dispatcher_for(ctx).run_once()


def test_an_epic_moved_to_done_gets_its_report_once_the_event_is_delivered(ctx, db, monkeypatch):
    add_ticket(db, "TESTWP-10")
    db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-10'")
    reported = []
    monkeypatch.setattr(project_lifecycle, "report_done_epic", lambda _ctx, project, key: reported.append((project, key)))

    merge_flow.record_move(ctx, "TESTWP-10", "In Progress", "Done", merge_flow.FLOW)
    assert reported == []  # the report is not generated inside the move
    _deliver(ctx)

    assert reported == [("TESTWP", "TESTWP-10")]


def test_a_task_moved_to_done_or_an_epic_already_done_gets_no_epic_report(ctx, db, monkeypatch):
    add_ticket(db, "TESTWP-11")
    add_ticket(db, "TESTWP-12")
    db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-12'")
    reported = []
    monkeypatch.setattr(project_lifecycle, "report_done_epic", lambda _ctx, project, key: reported.append(key))

    merge_flow.record_move(ctx, "TESTWP-11", "In Progress", "Done", merge_flow.FLOW)
    merge_flow.record_move(ctx, "TESTWP-12", "Done", "Done", merge_flow.FLOW)
    _deliver(ctx)

    assert reported == []


def test_a_failing_report_does_not_retry_the_delivery(ctx, db, monkeypatch):
    add_ticket(db, "TESTWP-13")
    db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-13'")

    def boom(*_args):
        raise RuntimeError("report store down")

    monkeypatch.setattr(project_lifecycle, "report_done_epic", boom)
    merge_flow.record_move(ctx, "TESTWP-13", "In Progress", "Done", merge_flow.FLOW)
    _deliver(ctx)

    assert db.one("SELECT status, attempts FROM domain_event_deliveries WHERE listener='tickets.report_done_epic'"
                  " ORDER BY event_id DESC LIMIT 1") == {"status": "done", "attempts": 1}


def test_entering_human_review_notifies_and_staying_in_it_does_not(ctx, db, monkeypatch):
    add_ticket(db, "TESTWP-14", labels=("require-human",))
    calls = []
    from aiflow.v4.control import human_review_notice
    monkeypatch.setattr(human_review_notice, "notify_human_review", lambda _ctx, key, ticket=None: calls.append(key))

    merge_flow.record_move(ctx, "TESTWP-14", "Human review", "Human review", merge_flow.FLOW)
    merge_flow.record_move(ctx, "TESTWP-14", "In Progress", "Human review", merge_flow.FLOW)
    _deliver(ctx)

    assert calls == ["TESTWP-14"]
