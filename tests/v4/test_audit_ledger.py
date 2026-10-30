"""The audit ledger as a listener (JAIF-475): an event that audits itself gets its write_audit row in its transaction."""
from dataclasses import dataclass

import pytest

from aiflow.v4.control import escalations as esc
from aiflow.v4.control.platform.events import (CATALOG, Audit, DomainEvent, Registry, event, publish)
from aiflow.v4.control.platform.events import audit_ledger


@pytest.fixture
def audited_event():
    before = dict(CATALOG)

    @event
    @dataclass(frozen=True)
    class WidgetChecked(DomainEvent):
        type = "demo.widget_checked"
        owner = "demo"
        aggregate = "widget"
        aggregate_field = "widget_id"
        widget_id: str
        by: str = ""

        def audit(self) -> Audit:
            return Audit("demo", "widget-check", self.widget_id, self.by, "checked", {"widget": self.widget_id})

    yield WidgetChecked
    CATALOG.clear()
    CATALOG.update(before)


def test_an_event_that_audits_itself_writes_one_audit_row_in_its_transaction(ctx, db, audited_event):
    registry = Registry()
    audit_ledger.register_listeners(ctx, registry)

    publish(db, audited_event(widget_id="W-1", by="person@example.test"), registry=registry)

    row = db.one("SELECT target, action, ticket, actor, accepted, result, payload FROM write_audit WHERE target='demo'")
    assert row == {"target": "demo", "action": "widget-check", "ticket": "W-1", "actor": "person@example.test",
                   "accepted": True, "result": "checked", "payload": {"widget": "W-1"}}


def test_the_audit_row_rolls_back_with_the_change(ctx, db, audited_event):
    registry = Registry()
    audit_ledger.register_listeners(ctx, registry)

    with pytest.raises(RuntimeError), db.transaction():
        publish(db, audited_event(widget_id="W-2"), registry=registry)
        raise RuntimeError("the change failed")

    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='demo'")["n"] == 0


def test_only_events_that_override_audit_get_a_listener(ctx, audited_event):
    types = audit_ledger.audited_types()

    assert "demo.widget_checked" in types and "decision.answered" in types and "ticket.moved" not in types


def test_answering_a_decision_audits_it_once_for_every_path(ctx, db):
    asked = esc.ask(ctx, "JOOWP-3", "JOOWP", "Which?", "Choose.", ["A", "B"], "A", "Pick one", ["pick"])
    decision_id = asked["decision"]["id"]

    esc.answer_decision(ctx, decision_id, "B", "", "owner@example.test")

    rows = db.all("SELECT actor, result, payload FROM write_audit WHERE target='escalation' AND action='decision-answer'")
    assert rows == [{"actor": "owner@example.test", "result": f"D{decision_id} Pick one",
                     "payload": {"decision_id": decision_id}}]
