"""Technical questions are decided by the SA, never by the owner (JAIF-369)."""
from __future__ import annotations

import pytest

from conftest import add_ticket
from aiflow.v4.control import escalations as esc
from aiflow.v4.core import technical_decision as td
from test_escalations import put_ticket, run_of

TECHNICAL = [
    "Should the failing date-dependent test be corrected in a separate change?",
    "Should I add a retry with a cache for the helper or keep the function as it is?",
    "Do we refactor the module first or add the feature to the existing class?",
    "Which order should the migration and the schema change be done in? Ordering of the work is unclear.",
    "Should I rename the helper and split the change into a follow-up?",
]
OWNER = [
    "Which payment provider should we pick for the new billing flow?",
    "Should we store the API key in the repository?",
    "Should the test run against production data?",
    "The ticket conflicts with AC2: should the endpoint return 404 or 410?",
    "Is the licensing of this library acceptable for customers?",
]


@pytest.mark.parametrize("text", TECHNICAL)
def test_engineering_choices_are_technical(text):
    assert td.is_technical(text)


@pytest.mark.parametrize("text", OWNER)
def test_owner_topics_are_never_technical(text):
    assert not td.is_technical(text)


class NoDB:
    def all(self, *a, **k):
        return []

    def one(self, *a, **k):
        return None


@pytest.mark.parametrize("text", TECHNICAL[:3])
def test_the_classifier_sends_a_technical_question_to_the_sa(text):
    c = esc.classify(NoDB(), text, "JAIF")
    assert c.cls == esc.TECHNICAL and not c.reaches_person


def test_owner_topics_still_reach_the_owner():
    assert esc.classify(NoDB(), OWNER[0], "JAIF").cls == esc.BUSINESS
    assert esc.classify(NoDB(), OWNER[3], "JAIF").cls == esc.BUSINESS


def test_the_sa_answer_is_settled_and_kept_as_precedent(ctx):
    question = TECHNICAL[0]
    put_ticket(ctx, "TESTWP-601", "Acceptance criteria:\n- AC1: fix the replay test")
    raised = esc.dev_question(ctx, run_of("TESTWP-601"), question)
    assert raised["class"] == esc.TECHNICAL and not raised["resolved"]
    answered = esc.triage_decision(ctx, run_of("TESTWP-601"), {
        "decision": "ANSWER", "answer": "Fix it in this change: a date-dependent test blocks the suite.",
        "why": "Options: separate change or fix here. A red suite blocks every ticket, so fix here.",
        "keywords": ["date-dependent", "replay test"], "topic": "date dependent tests"}, question)
    assert answered == {"action": "answered", "class": esc.TECHNICAL}
    precedent = ctx.db.one("SELECT source, decided_by, decision FROM owner_decisions WHERE status='decided' AND source='sa'")
    assert precedent["decided_by"] == "sa" and "Fix it in this change" in precedent["decision"]
    again = esc.classify(ctx.db, "Is the date-dependent replay test to be fixed here or separately?", "TESTWP")
    assert again.cls == esc.TAKEN


def test_the_sa_cannot_escalate_a_technical_decision(ctx):
    question = TECHNICAL[1]
    put_ticket(ctx, "TESTWP-602", "Acceptance criteria:\n- AC1: add the helper")
    esc.dev_question(ctx, run_of("TESTWP-602"), question)
    refused = esc.triage_decision(ctx, run_of("TESTWP-602"), {"decision": "ESCALATE", "why": "not sure"}, question)
    assert refused["action"] == "refused" and "never reaches a person" in refused["reason"]
