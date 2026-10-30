"""DecisionAnswered (JAIF-472): the owner's answer resumes the waiting tickets through a listener, with retry."""
import dataclasses

from aiflow.v4.control import escalations as esc
from aiflow.v4.control.features.escalation.events import DecisionAnswered
from aiflow.v4.control.platform.events import wiring


def test_answering_publishes_the_event_and_the_listener_resumes_at_once(ctx, db, monkeypatch):
    resumed = []
    monkeypatch.setattr(esc, "resume_waiting", lambda _ctx, decision, text, skip_ticket="": resumed.append(
        (decision["id"], text, decision["waiting_tickets"], skip_ticket)) or [])
    ask = esc.ask(ctx, "JOOWP-1", "JOOWP", "Which format?", "Choose.", ["CSV", "JSON"], "CSV", "Export format",
                  ["export"])
    decision_id = ask["decision"]["id"]

    esc.answer_decision(ctx, decision_id, "JSON", "", "owner")

    event = db.one("SELECT type, aggregate_id, payload FROM domain_events WHERE type='decision.answered'")
    assert event["aggregate_id"] == str(decision_id) and event["payload"]["answer"] == "JSON"
    assert [item[:2] for item in resumed] == [(decision_id, "JSON")]
    assert db.one("SELECT status FROM domain_event_deliveries WHERE listener='escalation.resume_waiting_tickets'")[
        "status"] == "done"


def test_a_failing_resume_is_left_for_the_dispatcher_to_retry(ctx, db, monkeypatch):
    calls = {"n": 0}

    def flaky(_ctx, decision, text, skip_ticket=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("jira down")
        return []

    monkeypatch.setattr(esc, "resume_waiting", flaky)
    ask = esc.ask(ctx, "JOOWP-2", "JOOWP", "Which?", "Choose.", ["A", "B"], "A", "Pick", ["pick"])
    decision_id = ask["decision"]["id"]

    esc.answer_decision(ctx, decision_id, "A", "", "owner")  # the answer is saved even though the resume failed

    row = db.one("SELECT status, attempts FROM domain_event_deliveries WHERE listener='escalation.resume_waiting_tickets'")
    assert (row["status"], row["attempts"]) == ("pending", 1)
    db.run("UPDATE domain_event_deliveries SET next_attempt_at=now() - interval '1 second'")
    wiring.dispatcher_for(ctx).run_once()
    assert calls["n"] == 2
    assert db.one("SELECT status FROM domain_event_deliveries WHERE listener='escalation.resume_waiting_tickets'")[
        "status"] == "done"


def test_the_event_carries_ids_and_text_but_no_secrets():
    names = {item.name for item in dataclasses.fields(DecisionAnswered)}
    assert names == {"decision_id", "answer", "answered_by", "waiting_tickets", "skip_ticket", "ticket_key", "title"}
