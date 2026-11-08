"""Assumptions the flow records instead of asking the owner (JAIF-338)."""
import uuid

from conftest import add_business_decision, add_ticket

from aiflow.v4.control.features.work.repository import WorkRepository
from aiflow.v4.control.repositories import assumptions as repo
from aiflow.v4.control.repositories.owner_decisions import business_decision_ticket_keys
from aiflow.v4.control.report import build_ticket_summary
from aiflow.v4.core import assumptions as core
from aiflow.v4.core.technical_decision import is_technical


def test_an_explicit_reason_marker_wins_over_punctuation_inside_the_assumption():
    text = "Assumptions:\n- split the module; keep one API; why: smaller diffs\n- use a flag — keeps rollout simple\n- none"
    assert core.parse_assumptions(text) == [
        {"assumption": "split the module; keep one API", "why": "smaller diffs"},
        {"assumption": "use a flag", "why": "keeps rollout simple"},
    ]


def test_verifier_findings_name_the_contradicted_assumptions_and_nothing_else():
    findings = ("- assumption: cache per request contradicts the stateless rule: it keeps state\n"
                "- the word contradicts appears here but names no assumption")
    assert core.contradicted_assumption_texts(findings) == ["cache per request"]
    assert core.contradicted_assumption_texts("") == []


def test_invalid_assumption_items_are_reported_before_a_run_is_completed():
    assert core.invalid_assumption_items([{"assumption": "ok"}]) == []
    assert core.invalid_assumption_items([{"assumption": "ok"}, {"why": "no text"}, "x"]) == [
        "assumption 2 needs a non-empty text", "assumption 3 needs a non-empty text"]
    assert core.invalid_assumption_items("nope") == ["assumptions must be a list"]


def test_recording_is_idempotent_and_keeps_contradiction_history(ctx):
    item = [{"assumption": "use a flag", "why": "rollout"}]
    assert len(repo.record_assumptions(ctx.db, "TESTWP-1", "TESTWP", "r1", "intake", item)) == 1
    assert repo.record_assumptions(ctx.db, "TESTWP-1", "TESTWP", "r2", "intake", item) == []
    assert repo.contradict_assumptions(ctx.db, "TESTWP-1", "r3", "breaks a rule", ["use a flag"]) == 1
    assert repo.record_assumptions(ctx.db, "TESTWP-1", "TESTWP", "r4", "intake", item) == []
    rows = repo.assumptions_for_ticket(ctx.db, "TESTWP-1")
    assert len(rows) == 1 and rows[0]["contradicted"] and rows[0]["contradiction_reason"] == "breaks a rule"


def test_only_the_named_assumptions_are_marked_contradicted(ctx):
    repo.record_assumptions(ctx.db, "TESTWP-2", "TESTWP", "r1", "intake",
                            [{"assumption": "A", "why": ""}, {"assumption": "B", "why": ""}])
    assert repo.contradict_assumptions(ctx.db, "TESTWP-2", "r2", "rule", []) == 0
    assert repo.contradict_assumptions(ctx.db, "TESTWP-2", "r2", "rule", ["B"]) == 1
    state = {row["assumption"]: row["contradicted"] for row in repo.assumptions_for_ticket(ctx.db, "TESTWP-2")}
    assert state == {"A": False, "B": True}


def test_the_dashboard_queries_respect_project_scope(ctx):
    repo.record_assumptions(ctx.db, "TESTWP-3", "TESTWP", "r1", "intake", [{"assumption": "mine", "why": ""}])
    repo.record_assumptions(ctx.db, "OTHER-3", "OTHER", "r1", "intake", [{"assumption": "theirs", "why": ""}])
    assert {row["assumption"] for row in repo.recent_assumptions(ctx.db)} == {"mine", "theirs"}
    assert [row["assumption"] for row in repo.recent_assumptions(ctx.db, projects=["TESTWP"])] == ["mine"]
    assert sum(day["total"] for day in repo.assumptions_daily(ctx.db, projects=["TESTWP"])) == 1
    assert repo.assumptions_daily(ctx.db, projects=[]) == []


def test_the_daily_window_starts_at_a_utc_day_boundary(ctx):
    repo.record_assumptions(ctx.db, "TESTWP-4", "TESTWP", "r1", "intake", [{"assumption": "old", "why": ""}])
    ctx.db.run("UPDATE assumptions SET created_at = date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
               " - interval '1 day' + interval '1 hour' WHERE assumption='old'")
    assert [day["total"] for day in repo.assumptions_daily(ctx.db, days=2)] == [1]
    assert repo.assumptions_daily(ctx.db, days=1) == []


def test_a_handed_off_decision_still_counts_as_a_business_wait(ctx):
    add_ticket(ctx.db, "TESTWP-5", status="Needs owner")
    add_ticket(ctx.db, "TESTWP-6", status="Needs owner")
    add_business_decision(ctx.db, "TESTWP-5")
    assert business_decision_ticket_keys(ctx.db) == {"TESTWP-5"}


def test_the_report_counts_only_business_needs_owner_tickets():
    tickets = [{"status": "Needs owner", "is_business_needs_owner": True, "type": "Story"},
               {"status": "Needs owner", "is_business_needs_owner": False, "type": "Story"}]
    assert build_ticket_summary(tickets, [])["needs_owner"] == 1


def test_a_business_limit_stays_with_the_owner_and_a_technical_one_does_not():
    assert not is_technical("What upload limit should the product allow for customers?")
    assert is_technical("Should the helper module be split into two classes?")


def test_technical_answers_are_counted_per_ticket_so_intake_can_stop_looping(ctx):
    for index in range(2):
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,status,outcome,account_id,node,fence) "
                   "VALUES (%s,'TESTWP-7','TESTWP','intake','finished','technical_choice_answered','a','n',1)",
                   (str(uuid.uuid4()),))
    assert WorkRepository(ctx.db).intake_technical_answers("TESTWP-7") == 2
    assert WorkRepository(ctx.db).intake_technical_answers("TESTWP-8") == 0
