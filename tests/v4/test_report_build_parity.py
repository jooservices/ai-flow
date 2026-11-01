"""Characterization of ``build_report``: literal results of the unchanged function.

Inputs cover every branch of the report (empty scope, unknown epic, parent chain, timings,
run outcomes, verify verdicts, acceptance criteria, owner signals, incidents and lessons).
Expected reports and database calls were recorded by running that function and live in
``tests/v4/fixtures/report_build_parity.json``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control import report as project_report
from aiflow.v4.control.report import ReportNotFound, build_report


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "report_build_parity.json"
AC = "Acceptance criteria:\nAC 1: ship it\n"

TICKETS_SQL = (
    "SELECT key, project, status, type, summary, description, parent_key, labels, jira_created, jira_updated, done_at"
    " FROM tickets WHERE project=%s ORDER BY key"
)
RUNS_SQL = (
    "SELECT ticket_key, step, account_id, provider, model, status, outcome, started_at, ended_at,"
    " tokens_in, tokens_out, cost_usd FROM runs WHERE ticket_key = ANY(%s) ORDER BY started_at"
)
REWORK_SQL = (
    "SELECT ticket_key, count(*) AS rounds FROM runs WHERE ticket_key = ANY(%s)"
    " AND step='verify' AND outcome='rework' GROUP BY ticket_key ORDER BY ticket_key"
)
VERIFY_SQL = (
    "SELECT DISTINCT ON (ticket_key) ticket_key, status, outcome, details, ended_at FROM runs"
    " WHERE ticket_key = ANY(%s) AND step='verify' AND status='finished'"
    " ORDER BY ticket_key, started_at DESC, ended_at DESC"
)
OWNER_SQL = (
    "SELECT ticket, count(*) AS requests FROM write_audit WHERE ticket = ANY(%s) AND accepted=true"
    " AND action='transition' AND (result='Needs owner' OR payload->>'to'='Needs owner')"
    " GROUP BY ticket ORDER BY ticket"
)
INCIDENTS_SQL = (
    "SELECT id, opened_at, severity, kind, subject AS ticket, title, detail FROM incidents"
    " WHERE subject = ANY(%s) ORDER BY opened_at"
)


def at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=UTC)


def ticket(key, *, project="PROJ", status="To Do", type="Task", summary=None, description="",
           parent=None, labels=None, created=None, updated=None, done=None):
    return {
        "key": key, "project": project, "status": status, "type": type,
        "summary": key if summary is None else summary, "description": description,
        "parent_key": parent, "labels": [] if labels is None else labels,
        "jira_created": created, "jira_updated": updated, "done_at": done,
    }


def run(key, *, step="dev", account="acct-a", provider="codex", model="gpt-5", status="finished",
        outcome="done", started, ended=None, tin=0, tout=0, cost=0.0, details=None):
    return {
        "ticket_key": key, "step": step, "account_id": account, "provider": provider, "model": model,
        "status": status, "outcome": outcome, "started_at": started, "ended_at": ended,
        "tokens_in": tin, "tokens_out": tout, "cost_usd": cost, "details": details,
    }


def audit(key, *, accepted=True, action="transition", result="", payload=None):
    return {"ticket": key, "accepted": accepted, "action": action, "result": result,
            "payload": {} if payload is None else payload}


def incident(key, *, id=1, opened, severity="warning", kind="run_stalled", title="Stalled", detail="detail"):
    return {"id": id, "opened_at": opened, "severity": severity, "kind": kind, "subject": key,
            "title": title, "detail": detail}


def case(name, tickets=(), runs=(), audits=(), incidents=(), epic=None, project="PROJ"):
    return {
        "name": name, "project": project, "epic": epic, "tickets": list(tickets), "runs": list(runs),
        "audits": list(audits), "incidents": list(incidents),
    }


def _desc(value):
    """Postgres ``ORDER BY col DESC`` puts nulls first."""
    if value is None:
        return (0,)
    return (1, -value.timestamp())


class FakeDb:
    """In-memory answers for the six queries ``build_report`` sends, in call order."""

    def __init__(self, data):
        self.tickets = data.get("tickets", [])
        self.runs = data.get("runs", [])
        self.audits = data.get("audits", [])
        self.incidents = data.get("incidents", [])
        self.calls = []

    def all(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == TICKETS_SQL:
            rows = [dict(row) for row in self.tickets if row["project"] == params[0]]
            rows.sort(key=lambda row: row["key"])
            return rows
        keys = set(params[0])
        if sql == RUNS_SQL:
            rows = [dict(row) for row in self.runs if row["ticket_key"] in keys]
            rows.sort(key=lambda row: (row["started_at"] is None, row["started_at"] or at(1)))
            return rows
        if sql == REWORK_SQL:
            counts = {}
            for row in self.runs:
                if row["ticket_key"] in keys and row["step"] == "verify" and row["outcome"] == "rework":
                    counts[row["ticket_key"]] = counts.get(row["ticket_key"], 0) + 1
            return [{"ticket_key": key, "rounds": counts[key]} for key in sorted(counts)]
        if sql == VERIFY_SQL:
            chosen = {}
            for row in self.runs:
                if row["ticket_key"] not in keys or row["step"] != "verify" or row["status"] != "finished":
                    continue
                rank = (row["ticket_key"], _desc(row["started_at"]), _desc(row["ended_at"]))
                if row["ticket_key"] not in chosen or rank < chosen[row["ticket_key"]][0]:
                    chosen[row["ticket_key"]] = (rank, row)
            return [{
                "ticket_key": row["ticket_key"], "status": row["status"], "outcome": row["outcome"],
                "details": row.get("details"), "ended_at": row["ended_at"],
            } for _, row in (chosen[key] for key in sorted(chosen))]
        if sql == OWNER_SQL:
            seen = []
            for row in self.audits:
                payload = row.get("payload") or {}
                if (row["ticket"] in keys and row.get("accepted") and row.get("action") == "transition"
                        and (row.get("result") == "Needs owner" or payload.get("to") == "Needs owner")):
                    seen.append(row["ticket"])
            return [{"ticket": key, "requests": seen.count(key)} for key in sorted(set(seen))]
        if sql == INCIDENTS_SQL:
            rows = [dict(row) for row in self.incidents if row["subject"] in keys]
            rows.sort(key=lambda row: (row["opened_at"] is None, row["opened_at"] or at(1)))
            return [{
                "id": row["id"], "opened_at": row["opened_at"], "severity": row["severity"],
                "kind": row["kind"], "ticket": row["subject"], "title": row["title"], "detail": row["detail"],
            } for row in rows]
        raise AssertionError(f"unexpected SQL: {sql!r}")


def recorded_calls(calls):
    encoded = []
    for sql, params in calls:
        encoded.append({
            "sql": sql,
            "params": [list(param) if isinstance(param, (list, tuple)) else param for param in (params or ())],
        })
    return encoded


def _ten_completed():
    return [ticket(f"T-{i:02d}", status="Done", summary=f"done {i}", created=at(1), done=at(1 + i))
            for i in range(1, 11)]


def _verify_tickets():
    """One Done ticket per verify-verdict branch, plus an open PASS and an ignored running run."""
    keys = [
        "V-BLOCK", "V-BLOCKED-OUT", "V-EMPTYDETAILS", "V-FAIL", "V-FAILED", "V-HUMAN", "V-LATEST",
        "V-LOST", "V-LOWER", "V-MAYBE", "V-MERGED", "V-NODETAILS", "V-NONE", "V-OPEN", "V-PASS",
        "V-QUOTA", "V-RELEASED", "V-REWORK", "V-RUNNING", "V-TIMEOUT",
    ]
    tickets = [ticket(key, status="To Do" if key == "V-OPEN" else "Done", description=AC) for key in keys]
    started = at(2)
    later = at(3)
    newest = at(4)
    runs = [
        run("V-MERGED", step="verify", outcome="merged", started=started, ended=later,
            details={"verdict": "FAIL"}),
        run("V-HUMAN", step="verify", outcome="human", started=started, ended=later, details={"verdict": "FAIL"}),
        run("V-REWORK", step="verify", outcome="rework", started=started, ended=later, details={"verdict": "PASS"}),
        run("V-PASS", step="verify", outcome="done", started=started, ended=later, details={"verdict": "PASS"}),
        run("V-FAIL", step="verify", outcome="done", started=started, ended=later, details={"verdict": "FAIL"}),
        run("V-BLOCK", step="verify", outcome="done", started=started, ended=later, details={"verdict": "BLOCKED"}),
        run("V-FAILED", step="verify", outcome="failed", started=started, ended=later, details={"verdict": "PASS"}),
        run("V-BLOCKED-OUT", step="verify", outcome="blocked", started=started, ended=later,
            details={"verdict": "PASS"}),
        run("V-TIMEOUT", step="verify", outcome="timeout", started=started, ended=later, details={"verdict": "PASS"}),
        run("V-LOST", step="verify", outcome="lost", started=started, ended=later, details={"verdict": "PASS"}),
        run("V-QUOTA", step="verify", outcome="quota", started=started, ended=later, details={"verdict": "PASS"}),
        run("V-RELEASED", step="verify", outcome="released", started=started, ended=later,
            details={"verdict": "PASS"}),
        run("V-MAYBE", step="verify", outcome="done", started=started, ended=later, details={"verdict": "maybe"}),
        run("V-NODETAILS", step="verify", outcome="done", started=started, ended=later, details=None),
        run("V-EMPTYDETAILS", step="verify", outcome="done", started=started, ended=later, details=""),
        run("V-LOWER", step="verify", outcome="Merged", started=started, ended=later, details={"verdict": "FAIL"}),
        run("V-LATEST", step="verify", outcome="merged", started=started, ended=later, details={"verdict": "PASS"}),
        run("V-LATEST", step="verify", outcome="rework", started=newest, ended=newest, details={"verdict": "PASS"}),
        run("V-RUNNING", step="verify", outcome="merged", status="running", started=newest, ended=None,
            details={"verdict": "PASS"}),
        run("V-OPEN", step="verify", outcome="done", started=started, ended=later, details={"verdict": "pass"}),
    ]
    return tickets, runs


_FULL_AC = """Ignore AC 9: before the section
## Acceptance criteria
AC 1: first criterion
- AC 2) second criterion
* AC 3 - third criterion
AC  4. fourth criterion
AC 5:
plain prose
### Technical notes:
AC 6: after the section
"""


CASES = [
    case("empty_project", tickets=[ticket("OTHER-1", project="OTHER", status="Done")]),
    case("blank_epic_key", epic="", tickets=[ticket("P-1", summary="still in scope")]),
    case("unknown_epic", epic="EP-MISSING", tickets=[ticket("P-1")]),
    case("story_is_not_epic", epic="P-1", tickets=[ticket("P-1", type="Story")]),
    case("lowercase_epic_type", epic="EP-1", tickets=[ticket("EP-1", type="epic", status="Done")]),
    case("epic_without_children", epic="EP-1", tickets=[ticket("EP-1", type="Epic", status="To Do")]),
    case("done_epics_without_children", tickets=[
        ticket("EP-1", type="Epic", status="Done"),
        ticket("EP-2", type="Epic", status="Done"),
    ]),
    case("partial_epics_with_work", tickets=[
        ticket("EP-1", type="Epic", status="Done"),
        ticket("EP-2", type="Epic", status="To Do"),
        ticket("P-1", parent="EP-1", summary="only child"),
    ]),
    case("all_epics_done_with_work", tickets=[
        ticket("EP-1", type="Epic", status="Done"),
        ticket("EP-2", type="Epic", status="Done"),
        ticket("P-1", status="Done", parent="EP-1", summary="shipped", created=at(1), done=at(2)),
    ]),
    case("parent_chain_scope", epic="EP", tickets=[
        ticket("CHILD-EP", type="Epic", status="Done", parent="EP"),
        ticket("CYC1", parent="CYC2"),
        ticket("CYC2", parent="CYC1"),
        ticket("EP", type="Epic", status="To Do"),
        ticket("GONE", parent="NOPE"),
        ticket("OTHER", type="Epic", status="To Do"),
        ticket("OUT", type="Story", parent="OTHER"),
        ticket("OUT2", parent="OUT"),
        ticket("S1", type="Story", parent="EP"),
        ticket("S2", type="Story", parent="EP"),
        ticket("SELF", parent="SELF"),
        ticket("T1", parent="S1"),
        ticket("T3", parent="S2"),
        ticket("TOP"),
    ]),
    case("status_counts", tickets=[
        ticket("EP-1", type="Epic", status="Done"),
        ticket("P-BLOCK", status="Blocked"),
        ticket("P-BUG", type="Bug", status="Done"),
        ticket("P-DONE", type="Task", status="Done"),
        ticket("P-HUMAN", status="Human review"),
        ticket("P-LOWER", status="in progress"),
        ticket("P-OWNER", status="Needs owner", summary="ask the owner"),
        ticket("P-PROG", status="In Progress"),
        ticket("P-STORY", type="Story", status="Done"),
        ticket("P-TODO", status="To Do"),
        ticket("P-VERIFY", status="In Verify"),
    ]),
    case("period_and_lead_edges", tickets=[
        ticket("EDGE-1", status="Done", created=at(2), updated=at(9), done=at(5), summary="prefers done_at"),
        ticket("EDGE-2", status="Done", created=at(3), updated=at(4), summary="falls back to jira_updated"),
        ticket("EDGE-3", status="Done", summary="no timestamps"),
        ticket("EDGE-4", created=at(1), summary="open ticket sets the start"),
        ticket("EDGE-5", status="Done", created=at(8), done=at(6), summary="completion before creation"),
    ]),
    case("negative_period", tickets=[
        ticket("N-DONE", status="Done", created=at(18), done=at(10), summary="backwards"),
        ticket("N-OPEN", created=at(20), summary="later creation"),
    ]),
    case("two_completed_p90", tickets=[
        ticket("A-1", status="Done", created=at(1), done=at(1, 1), summary="one hour"),
        ticket("B-1", status="Done", created=at(1), done=at(1, 3), summary="three hours"),
    ]),
    case("ten_completed_p90_and_slowest", tickets=_ten_completed()),
    case("run_outcome_fallback", tickets=[ticket("P-1")], runs=[
        run("P-1", outcome="done", status="failed", started=at(1, 1)),
        run("P-1", outcome="", status="released", started=at(1, 2)),
        run("P-1", outcome=None, status=None, started=at(1, 3)),
        run("P-1", outcome="", status="", started=at(1, 4)),
    ]),
    case("usage_sort_and_rounding", tickets=[ticket("P-1"), ticket("P-2")], runs=[
        run("P-1", account="a", provider="alpha", model="m1", started=at(1, 1), tin=1, tout=2, cost=0.0000004),
        run("P-1", account="a", provider="alpha", model="m1", started=at(1, 2), tin=1, tout=2, cost=0.0000004),
        run("P-1", account="a", provider="alpha", model="m1", started=at(1, 3), tin=1, tout=2, cost=0.0000004),
        run("P-2", account="b", provider="zeta", model="m2", started=at(1, 4), tin=10, tout=20,
            cost=Decimal("1.25")),
    ]),
    case("ai_time_sample_versus_total", tickets=[
        ticket("D-1", status="Done", created=at(1), done=at(2), summary="completed"),
        ticket("O-1", status="In Progress", summary="still open"),
    ], runs=[
        run("D-1", started=at(1), ended=at(1, 2)),
        run("D-1", started=at(1, 5), ended=at(1, 4)),
        run("D-1", started=at(1, 6), ended=None),
        run("O-1", started=at(1), ended=at(1, 3)),
    ]),
    case("verify_verdicts", *_verify_tickets()),
    case("acceptance_criteria_text", tickets=[
        ticket("AC-BUS", description="## Acceptance criteria\nAC 1: kept\nBusiness requirement: stop\nAC 2: dropped\n"),
        ticket("AC-DEF", description="## Acceptance criteria\nAC 1: kept\ndefinition of done: stop\nAC 2: dropped\n"),
        ticket("AC-EMPTY", description=""),
        ticket("AC-FULL", description=_FULL_AC),
        ticket("AC-HASH", description="### ACCEPTANCE CRITERIA\nAC 7: upper heading\n# Next section\nAC 8: hidden\n"),
        ticket("AC-ORPHAN", description="AC 1: orphan\n"),
        ticket("AC-PLAIN", description="Acceptance criteria:\nAC1: colon heading\n"),
        ticket("AC-TEST", description="## Acceptance criteria\nAC 1: kept\nTest cases: stop here\nAC 2: dropped\n"),
    ]),
    case("coverage_gaps", tickets=[
        ticket("G-DONE-BARE", status="Done"),
        ticket("G-DONE-FAIL", status="Done", description=AC),
        ticket("G-DONE-PASS", status="Done", description=AC),
        ticket("G-OPEN-AC", description=AC),
        ticket("G-OPEN-BARE"),
    ], runs=[
        run("G-DONE-FAIL", step="verify", outcome="done", started=at(2), ended=at(3), details={"verdict": "FAIL"}),
        run("G-DONE-PASS", step="verify", outcome="done", started=at(2), ended=at(3), details={"verdict": "PASS"}),
    ]),
    case("owner_needed_sources", tickets=[
        ticket("A-AUDIT", summary="from the audit result"),
        ticket("B-PAYLOAD", status="In Progress", summary="from the payload"),
        ticket("C-STATUS", status="Needs owner", summary="from the status"),
        ticket("D-LABEL", labels=["other", "needs-owner"], summary="from the label"),
        ticket("E-ALL", status="Needs owner", labels=["needs-owner"], summary="counted once"),
        ticket("F-REJECTED"),
        ticket("G-COMMENT"),
        ticket("H-CLEAN", status="Done"),
        ticket("I-SIMILAR", labels=["needs-owner-extra"]),
    ], audits=[
        audit("A-AUDIT", result="Needs owner"),
        audit("B-PAYLOAD", result="In Progress", payload={"to": "Needs owner"}),
        audit("E-ALL", result="Needs owner"),
        audit("F-REJECTED", accepted=False, result="Needs owner"),
        audit("G-COMMENT", action="comment", result="Needs owner"),
        audit("H-CLEAN", result="Done", payload={"to": "Done"}),
    ]),
    case("incidents_and_three_lessons", tickets=[
        ticket("P-OWNER", status="Needs owner", summary="ask"),
        ticket("P-REWORK", summary="again"),
    ], runs=[
        run("P-REWORK", step="verify", outcome="rework", started=at(2), ended=at(3)),
    ], incidents=[
        incident("P-REWORK", id=2, opened=at(4), title="Second", kind="account_quota"),
        incident("P-OWNER", id=1, opened=at(2), title="First", detail="needs a person"),
    ]),
    case("incident_with_no_opened_at", tickets=[ticket("P-1")], incidents=[
        incident("P-1", opened=None, title="Undated", kind="node_down"),
    ]),
    case("populated_quiet_history", tickets=[
        ticket("P-1", status="Done", summary="quiet", created=at(1), done=at(2)),
    ]),
    case("rework_rounds_grouped", tickets=[ticket("R-1"), ticket("R-2")], runs=[
        run("R-1", step="verify", outcome="rework", started=at(1), ended=at(2)),
        run("R-1", step="verify", outcome="rework", started=at(3), ended=at(4)),
        run("R-1", step="dev", outcome="rework", started=at(1, 3), ended=at(1, 4)),
        run("R-2", step="verify", outcome="rework", started=at(2), ended=at(3)),
        run("R-2", step="verify", outcome="done", started=at(5), ended=at(6), details={"verdict": "PASS"}),
    ]),
    case("single_rework_lesson", tickets=[ticket("R-1", description=AC)], runs=[
        run("R-1", step="verify", outcome="rework", started=at(2), ended=at(3), details={"verdict": "PASS"}),
    ]),
]


def test_characterization_covers_at_least_twenty_inputs():
    assert len(CASES) >= 20
    assert len({row["name"] for row in CASES}) == len(CASES)


@pytest.mark.parametrize("row", CASES, ids=[row["name"] for row in CASES])
def test_build_report_matches_recorded_result(row):
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))[row["name"]]
    db = FakeDb(row)
    if "error" in expected:
        with pytest.raises(ReportNotFound) as caught:
            build_report(SimpleNamespace(db=db), row["project"], row["epic"])
        assert str(caught.value) == expected["error"]
    else:
        actual = build_report(SimpleNamespace(db=db), row["project"], row["epic"])
        assert list(actual) == list(expected["report"])
        assert actual == expected["report"]
    assert recorded_calls(db.calls) == expected["calls"]


def _ctx(data):
    return SimpleNamespace(db=FakeDb(data))


def test_load_project_tickets_keeps_one_project_in_key_order():
    ctx = _ctx(case("load", tickets=[ticket("B"), ticket("A", project="OTHER"), ticket("C")]))
    assert [row["key"] for row in project_report.load_project_tickets(ctx, "PROJ")] == ["B", "C"]
    assert ctx.db.calls[0][0] == TICKETS_SQL


def test_build_project_close_suggestion_requires_every_epic_done():
    open_epic = [ticket("E1", type="Epic", status="Done"), ticket("E2", type="Epic"), ticket("P1")]
    assert project_report.build_project_close_suggestion(open_epic) == {
        "suggested": False, "epics_done": 1, "epics_total": 2}
    assert project_report.build_project_close_suggestion([ticket("P1")])["suggested"] is False
    done = [ticket("E1", type="Epic", status="Done"), ticket("E2", type="Epic", status="Done")]
    assert project_report.build_project_close_suggestion(done)["suggested"] is True


def test_require_epic_rejects_a_missing_or_non_epic_key():
    by_key = {"P-1": ticket("P-1", type="Story"), "EP": ticket("EP", type="Epic")}
    project_report.require_epic(by_key, "PROJ", "EP")
    with pytest.raises(ReportNotFound, match="unknown epic P-1 in project PROJ"):
        project_report.require_epic(by_key, "PROJ", "P-1")
    with pytest.raises(ReportNotFound, match="unknown epic NOPE in project PROJ"):
        project_report.require_epic(by_key, "PROJ", "NOPE")


def test_tickets_in_scope_follows_parents_and_stops_on_a_cycle():
    rows = [
        ticket("EP", type="Epic"), ticket("S", type="Story", parent="EP"), ticket("T", parent="S"),
        ticket("A", parent="B"), ticket("B", parent="A"), ticket("CHILD", type="Epic", parent="EP"),
    ]
    by_key = {row["key"]: row for row in rows}
    assert [row["key"] for row in project_report.tickets_in_scope(rows, "EP", by_key)] == ["S", "T"]
    assert [row["key"] for row in project_report.tickets_in_scope(rows, None, by_key)] == ["S", "T", "A", "B"]


def test_completion_times_prefers_done_at_then_jira_updated():
    explicit = ticket("D", status="Done", updated=at(9), done=at(2))
    fallback = ticket("E", status="Done", updated=at(4))
    missing = ticket("F", status="Done")
    assert project_report.completion_times([explicit, fallback, missing]) == {
        "D": at(2), "E": at(4), "F": None}


def test_load_runs_returns_scoped_runs_oldest_first():
    ctx = _ctx(case("runs", runs=[
        run("P-1", started=at(3)), run("Q-1", started=at(2)), run("P-1", started=at(1)),
    ]))
    loaded = project_report.load_runs(ctx, ["P-1"])
    assert [row["started_at"] for row in loaded] == [at(1), at(3)]
    assert ctx.db.calls[0][0] == RUNS_SQL


def test_summarize_runs_counts_unknown_and_rounds_each_added_cost():
    rows = [
        run("P-1", outcome="", status="", started=at(1), tin=1, tout=1, cost=0.0000004),
        run("P-1", account="b", provider="zeta", model="m2", outcome="done", started=at(1), ended=at(1, 1),
            tin=2, tout=3, cost=0.5),
    ]
    summary = project_report.summarize_runs(rows)
    assert summary["run_outcomes"] == {"unknown": 1, "done": 1}
    assert summary["usage_by_ticket"]["P-1"]["cost_usd"] == 0.5
    assert summary["total_in"] == 3 and summary["total_out"] == 4
    assert summary["ai_seconds_by_ticket"]["P-1"] == 3600.0
    assert [row["account"] for row in summary["account_totals"].values()] == ["acct-a", "b"]


def test_completion_samples_clamps_a_negative_lead_and_skips_other_tickets_ai_time():
    done = ticket("D", status="Done", summary="back", created=at(5), done=at(4))
    lead, slow, ai = project_report.completion_samples(
        [done], project_report.completion_times([done]), {"D": 3.0, "OTHER": 9.0})
    assert lead == [0.0]
    assert slow[0]["lead_time_seconds"] == 0.0 and slow[0]["done_at"].startswith("2024-01-04")
    assert ai == [3.0]


def test_lead_time_by_ticket_rounds_to_one_decimal():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=1.26)
    done = ticket("D", status="Done", created=start, done=end)
    assert project_report.lead_time_by_ticket([done], {"D": end}) == {"D": 1.3}
    assert project_report.lead_time_by_ticket([ticket("E", status="Done")], {"E": None}) == {}


def test_load_rework_counts_only_verify_rework_runs():
    ctx = _ctx(case("rework", runs=[
        run("R-1", step="verify", outcome="rework", started=at(1), ended=at(2)),
        run("R-1", step="verify", outcome="rework", started=at(3), ended=at(4)),
        run("R-1", step="dev", outcome="rework", started=at(1)),
        run("R-2", step="verify", outcome="done", started=at(1), ended=at(2)),
    ]))
    rows, by_ticket = project_report.load_rework(ctx, ["R-1", "R-2"])
    assert rows == [{"key": "R-1", "rounds": 2}]
    assert by_ticket == {"R-1": 2}
    assert ctx.db.calls[0][0] == REWORK_SQL


def test_load_latest_verify_keeps_the_newest_finished_run():
    ctx = _ctx(case("verify", runs=[
        run("P-1", step="verify", outcome="merged", started=at(1), ended=at(2), details={"verdict": "PASS"}),
        run("P-1", step="verify", outcome="rework", started=at(4), ended=at(4), details={"verdict": "PASS"}),
        run("P-1", step="verify", outcome="merged", status="running", started=at(5), details={"verdict": "PASS"}),
    ]))
    latest = project_report.load_latest_verify(ctx, ["P-1"])
    assert latest["P-1"]["outcome"] == "rework"
    assert latest["P-1"]["ended_at"] == at(4)
    assert ctx.db.calls[0][0] == VERIFY_SQL


def test_load_owner_needed_merges_audit_status_and_label_once():
    rows = [
        ticket("A-AUDIT", summary="audit"),
        ticket("B-LABEL", labels=["needs-owner"], summary="label"),
        ticket("C-STATUS", status="Needs owner", summary="status"),
        ticket("D-EXTRA", labels=["needs-owner-extra"], summary="not a label"),
    ]
    ctx = _ctx(case("owner", tickets=rows, audits=[
        audit("A-AUDIT", result="Needs owner"),
        audit("C-STATUS", result="Needs owner"),
        audit("D-EXTRA", accepted=False, result="Needs owner"),
    ]))
    by_key = {row["key"]: row for row in rows}
    needed = project_report.load_owner_needed(ctx, [row["key"] for row in rows], rows, by_key)
    assert [row["key"] for row in needed] == ["A-AUDIT", "B-LABEL", "C-STATUS"]
    assert ctx.db.calls[0][0] == OWNER_SQL


def test_load_incidents_renders_opened_at_on_the_returned_row():
    ctx = _ctx(case("incidents", incidents=[
        incident("P-1", id=2, opened=None, title="Undated"),
        incident("P-1", id=1, opened=at(2), title="Dated"),
    ]))
    rows = project_report.load_incidents(ctx, ["P-1"])
    assert [(row["id"], row["opened_at"]) for row in rows] == [
        (1, "2024-01-02T00:00:00+00:00"), (2, None)]
    assert ctx.db.calls[0][0] == INCIDENTS_SQL


def test_build_period_allows_a_negative_span_and_an_empty_one():
    done = ticket("N", status="Done", created=at(18), done=at(10))
    period = project_report.build_period(
        [done, ticket("O", created=at(20))], [done], project_report.completion_times([done]))
    assert period["days"] == -8.0
    assert period["start"] == "2024-01-18T00:00:00+00:00"
    assert period["end"] == "2024-01-10T00:00:00+00:00"
    bare = ticket("B", status="Done")
    assert project_report.build_period([bare], [bare], {"B": None}) == {
        "start": None, "end": None, "days": None}


def test_build_ticket_summary_counts_active_statuses_and_sorts_types():
    rows = [
        ticket("A", status="Done", type="Task"), ticket("B", status="Done", type="Bug"),
        ticket("C", status="In Progress"), ticket("D", status="In Verify"),
        ticket("E", status="Human review"), ticket("F", status="Needs owner"),
        ticket("G", status="Blocked"),
    ]
    done = [row for row in rows if row["status"] == "Done"]
    assert project_report.build_ticket_summary(rows, done) == {
        "total": 7, "done": 2, "in_progress": 3, "needs_owner": 1,
        "done_by_type": {"Bug": 1, "Task": 1}}


def test_build_lead_time_reports_an_empty_sample_and_the_p90_rank():
    assert project_report.build_lead_time([]) == {
        "sample_size": 0, "median_seconds": None, "p90_seconds": None}
    assert project_report.build_lead_time([3600.0, 10800.0]) == {
        "sample_size": 2, "median_seconds": 7200.0, "p90_seconds": 10800.0}


def test_build_ai_time_adds_open_ticket_seconds_to_the_total_only():
    stats = project_report.build_ai_time([7200.0], {"D": 7200.0, "O": 10800.0})
    assert stats["sample_size"] == 1
    assert stats["median_seconds"] == 7200.0
    assert stats["total_seconds"] == 18000.0
    assert project_report.build_ai_time([], {})["total_seconds"] == 0


def test_build_runs_by_outcome_sorts_the_keys():
    from collections import Counter
    assert project_report.build_runs_by_outcome(Counter({"released": 1, "done": 2})) == {
        "done": 2, "released": 1}


def test_build_rework_rounds_sums_the_per_ticket_counts():
    rows = [{"key": "R-1", "rounds": 2}, {"key": "R-2", "rounds": 1}]
    assert project_report.build_rework_rounds(rows) == {"count": 3, "tickets": rows}
    assert project_report.build_rework_rounds([]) == {"count": 0, "tickets": []}


def test_build_usage_sorts_accounts_and_models_and_rounds_the_total():
    built = project_report.build_usage({
        "total_in": 3, "total_out": 6, "total_cost": 1.2500012,
        "account_totals": {
            "b": {"account": "b", "runs": 1},
            "a": {"account": "a", "runs": 3, "cost_usd": 0.0},
        },
        "model_totals": {
            ("zeta", "m2"): {"provider": "zeta", "model": "m2"},
            ("alpha", "m1"): {"provider": "alpha", "model": "m1"},
        },
    })
    assert built["tokens_total"] == 9
    assert built["cost_usd"] == 1.250001
    assert [row["account"] for row in built["by_account"]] == ["a", "b"]
    assert [(row["provider"], row["model"]) for row in built["by_model"]] == [("alpha", "m1"), ("zeta", "m2")]


def test_build_incidents_opened_counts_the_rows():
    rows = [{"id": 1}, {"id": 2}]
    assert project_report.build_incidents_opened(rows) == {"count": 2, "items": rows}


def test_build_owner_needed_counts_the_rows():
    rows = [{"key": "A", "summary": "ask"}]
    assert project_report.build_owner_needed(rows) == {"count": 1, "tickets": rows}
    assert project_report.build_owner_needed([]) == {"count": 0, "tickets": []}


def test_build_slowest_tickets_keeps_five_longest_and_breaks_ties_by_key():
    rows = [{"key": key, "lead_time_seconds": seconds} for key, seconds in (
        ("B", 5.0), ("A", 5.0), ("C", 1.0), ("D", 9.0), ("E", 8.0), ("F", 7.0), ("G", 6.0))]
    assert [row["key"] for row in project_report.build_slowest_tickets(rows)] == ["D", "E", "F", "G", "A"]


def test_build_lessons_joins_each_reason_and_uses_the_filled_quiet_sentence():
    quiet = "The recorded history shows no rework, owner escalation, or incident; keep the successful workflow."
    assert project_report.build_lessons([], [], []) == [quiet]
    lessons = project_report.build_lessons([{"rounds": 1}], [{"key": "A"}], [{"id": 1}])
    assert lessons == [
        "Clarify acceptance criteria for the 1 ticket(s) that needed 1 rework round(s).",
        "Bring owner decisions into intake earlier for the 1 ticket(s) that needed owner input.",
        "Review the 1 recorded incident(s) and their causes before repeating this project setup.",
    ]


def test_empty_report_keeps_its_own_lesson_and_key_order():
    suggestion = {"suggested": True, "epics_done": 1, "epics_total": 1}
    report = project_report.empty_report("PROJ", "EP", suggestion, [])
    assert list(report)[-2:] == ["project_close_suggestion", "lessons"]
    assert report["lessons"] == ["No lessons can be inferred from the recorded project history yet."]
    assert report["project_close_suggestion"] is suggestion
    assert report["requirement_coverage"]["gaps"] == [{"ticket": "EP", "reason": "epic_has_no_child_tickets"}]
    assert report["ai_time"]["total_seconds"] == 0
    assert report["tickets"]["total"] == 0


def test_filled_report_reads_history_then_puts_lessons_last():
    rows = [ticket("P-1", status="Done", summary="shipped", created=at(1), done=at(2))]
    ctx = _ctx(case("filled", tickets=rows, runs=[
        run("P-1", outcome="done", started=at(1), ended=at(1, 1), tin=4, tout=6, cost=1.5),
    ]))
    suggestion = {"suggested": False, "epics_done": 0, "epics_total": 0}
    report = project_report.filled_report(ctx, "PROJ", None, {"P-1": rows[0]}, suggestion, rows)
    assert [call["sql"] for call in recorded_calls(ctx.db.calls)] == [
        RUNS_SQL, REWORK_SQL, VERIFY_SQL, OWNER_SQL, INCIDENTS_SQL]
    assert list(report)[2] == "project_close_suggestion"
    assert list(report)[-1] == "lessons"
    assert report["usage"]["tokens_total"] == 10
    assert report["lessons"][0].startswith("The recorded history shows no rework")
