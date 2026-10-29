"""An unanswered owner question with a reversible recommendation is applied after its deadline (JAIF-370)."""
from __future__ import annotations

from aiflow.v4.control import escalation_deadline as deadline
from aiflow.v4.control import escalations as esc
from test_escalations import put_ticket, run_of

BUSINESS_Q = "Failed payment email: send it now or after a day?"
IRREVERSIBLE_Q = "Deploy this change to production?"


def ask(ctx, key, question, recommended="Send it after one day", topic="payment email timing"):
    put_ticket(ctx, key, "Acceptance criteria:\n- AC1: send the failed payment email")
    run = run_of(key)
    raised = esc.dev_question(ctx, run, question)
    asked = esc.triage_decision(ctx, run, {"decision": "ESCALATE", "class": raised["class"], "why": question,
                                           "options": ["Send it now", "Send it after one day"], "recommended": recommended,
                                           "topic": topic, "keywords": [topic]}, question)
    assert asked["action"] == "asked"
    return asked["decision_id"], raised["class"]


def age(ctx, decision_id, hours):
    ctx.db.run("UPDATE owner_decisions SET created_at = now() - make_interval(hours => %s) WHERE id = %s", (hours, decision_id))


def row(ctx, decision_id):
    return ctx.db.one("SELECT status, decision, decided_by FROM owner_decisions WHERE id = %s", (decision_id,))


def test_a_question_before_its_deadline_keeps_waiting(ctx):
    decision_id, cls = ask(ctx, "TESTWP-701", BUSINESS_Q)
    assert cls == esc.BUSINESS
    age(ctx, decision_id, deadline.DEFAULT_DEADLINE_HOURS - 1)
    assert deadline.apply_due(ctx) == []
    assert row(ctx, decision_id)["status"] == "asked"


def test_an_overdue_reversible_question_is_decided_with_the_recommendation(ctx):
    decision_id, _ = ask(ctx, "TESTWP-702", BUSINESS_Q)
    age(ctx, decision_id, deadline.DEFAULT_DEADLINE_HOURS + 1)
    assert deadline.apply_due(ctx) == [decision_id]
    decided = row(ctx, decision_id)
    assert decided["status"] == "decided" and decided["decided_by"] == "deadline"
    assert decided["decision"].startswith("Send it after one day") and "reverse it" in decided["decision"]
    assert deadline.apply_due(ctx) == []  # nothing left to apply


def test_an_irreversible_question_is_never_applied_and_opens_one_reminder(ctx):
    decision_id, cls = ask(ctx, "TESTWP-703", IRREVERSIBLE_Q, recommended="Do not deploy", topic="deploy to production")
    assert cls == esc.IRREVERSIBLE
    age(ctx, decision_id, 48)
    assert deadline.apply_due(ctx) == [] and deadline.apply_due(ctx) == []
    assert row(ctx, decision_id)["status"] == "asked"
    reminders = ctx.db.all("SELECT subject FROM incidents WHERE kind='owner_question_overdue' AND closed_at IS NULL")
    assert [r["subject"] for r in reminders] == [f"D{decision_id}"]


def test_a_question_without_a_recommendation_keeps_waiting(ctx):
    decision_id, _ = ask(ctx, "TESTWP-704", BUSINESS_Q, recommended="", topic="no recommendation")
    ctx.db.run("UPDATE owner_decisions SET recommended='' WHERE id = %s", (decision_id,))
    age(ctx, decision_id, 24)
    assert deadline.apply_due(ctx) == []
    assert row(ctx, decision_id)["status"] == "asked"


def test_the_deadline_is_a_parameter(ctx):
    decision_id, _ = ask(ctx, "TESTWP-705", BUSINESS_Q)
    age(ctx, decision_id, 3)
    assert deadline.apply_due(ctx, hours=2) == [decision_id]
