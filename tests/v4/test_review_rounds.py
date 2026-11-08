"""JAIF-351 review-round behavior and its backwards-compatible legacy input."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from conftest import add_ticket
from aiflow.v4.control.db import J
from aiflow.v4.control.features.review_rounds import models as review_round_models
from aiflow.v4.control.features.review_rounds import repository as review_round_repository
from aiflow.v4.control.features.review_rounds import service as review_round_metrics
from aiflow.v4.control.features.work.claim_extras import verify_extras
from aiflow.v4.core import review_findings
from aiflow.v4.control.features.work.eligibility_candidates import (
    _verify_candidate_ready,
    first_review_account,
    intake_candidates,
    preferred_review_available,
    verify_candidates,
)
from aiflow.v4.worker.verify import findings_of
from aiflow.v4.worker import intake, verify


def test_review_run_model_keeps_the_typed_ticket_and_details():
    run = review_round_models.ReviewRun.from_row({"ticket_key": "TESTWP-1", "details": {"review_round": 2}})

    assert run == review_round_models.ReviewRun("TESTWP-1", {"review_round": 2})


def test_legacy_verify_findings_keep_their_original_bullets():
    text = """CHECKS: pytest
VERDICT: FAIL
FINDINGS:
- retry path drops the state
* missing validation for empty input
"""

    assert findings_of(text) == "- retry path drops the state\n* missing validation for empty input"


def test_legacy_verify_without_findings_returns_an_empty_string():
    assert findings_of("CHECKS: pytest\nVERDICT: PASS\nFINDINGS:\n") == ""


def test_verifier_prompt_receives_history_and_requires_answers_to_be_checked():
    claim = SimpleNamespace(
        project={"base": "develop", "checks": ["pytest"], "containers": False, "environment": []},
        ticket={"key": "TESTWP-1", "project": "TESTWP", "summary": "Retry safely",
                "description": "Acceptance criteria:\n- AC1: retries preserve the token"},
        related=[], context_pack={},
    )
    pr = {"number": 7, "branch": "test-branch", "review_history": [
        {"id": "F1", "wording": "retry drops the token", "status": "not_fixed", "first_round": 1}],
          "dev_summary": "FINDING ANSWERS:\n- F1: kept the token across retry"}

    prompt = verify.build_prompt(claim, pr, env={})

    assert "F1 [NOT FIXED]: retry drops the token" in prompt
    assert "F1: kept the token across retry" in prompt
    assert "developer's matching `FINDING ANSWERS` entry" in prompt
    assert "Report every blocking finding together" in prompt
    assert "CHECKED:" in prompt


def test_review_parser_repeats_unresolved_wording_and_counts_new_round_findings():
    previous = [{"id": "F1", "wording": "retry drops the token on timeout", "status": "new", "first_round": 1}]
    response = SimpleNamespace(final_text="""CHECKS: pytest
REVIEW FINDINGS:
- F1 [NOT FIXED]: retry drops the token after timeout
- NEW: empty input is accepted
VERDICT: FAIL
FINDINGS:
- 1. F1: retry drops the token after timeout
- 2. F2: empty input is accepted
""")

    review = verify.parse_verify_review(response, previous, 2)

    assert review.findings == "- 1. F1: retry drops the token on timeout\n- 2. F2: empty input is accepted"
    assert review.review_findings[0]["wording"] == "retry drops the token on timeout"
    assert review.new_findings == 1 and review.review_round == 2


def test_verification_fails_when_a_prior_finding_is_still_active(monkeypatch):
    response = SimpleNamespace(final_text="""CHECKS: pytest
REVIEW FINDINGS:
- F1 [NOT FIXED]
VERDICT: PASS
""")
    review = verify.parse_verify_review(response, [
        {"id": "F1", "wording": "retry drops the token", "status": "new", "first_round": 1}], 2)
    monkeypatch.setattr(verify, "rework_verify", lambda _state, findings, why: (findings, why))
    state = SimpleNamespace(claim=SimpleNamespace(project={"checks": []}))

    assert verify.collect_verification(state, object(), review) == (
        "- 1. F1: retry drops the token", "verify failed")


def test_review_without_a_verdict_keeps_the_existing_failure_path(monkeypatch):
    response = SimpleNamespace(final_text="REVIEW FINDINGS:\n- F1 [NOT FIXED]\n", error_kind="",
                               error_message="",)
    review = verify.parse_verify_review(response, [
        {"id": "F1", "wording": "retry drops the token", "status": "new", "first_round": 1}], 2)
    state = SimpleNamespace(claim=SimpleNamespace(project={"checks": []}), result={})
    monkeypatch.setattr(verify, "rework_verify", lambda *_args: (_ for _ in ()).throw(AssertionError("reworked")))

    result = verify.collect_verification(state, object(), review)

    assert result["outcome"] == "failed" and result["summary"].startswith("no verdict")


def test_first_review_account_is_read_from_completed_review_history():
    calls = []
    scope = SimpleNamespace(ctx=SimpleNamespace(db=SimpleNamespace(
        one=lambda query, params: calls.append((query, params)) or {"account_id": "codex-01"})))

    assert first_review_account(scope, "TESTWP-1") == "codex-01"
    assert calls[0][1] == ("TESTWP-1",) and "details->>'verdict'" in calls[0][0]


def test_preferred_reviewer_availability_uses_normal_ticket_eligibility():
    account = SimpleNamespace(machines=["node-a", "node-b"])
    calls = []
    scope = SimpleNamespace(
        repo=SimpleNamespace(account=lambda key: account if key == "codex-01" else None),
        deps=SimpleNamespace(eligible=lambda *args, **kwargs:
                             calls.append((args, kwargs)) or ([{"key": "TESTWP-1", "step": "verify"}]
                                                              if args[1] == "node-b" else [])))

    assert preferred_review_available(scope, "codex-01", {"key": "TESTWP-1"})
    assert [item[0][1] for item in calls] == ["node-a", "node-b"]
    assert calls[0][1] == {"routing": False, "capacity_routing": False,
                           "ticket_key": "TESTWP-1", "step_filter": "verify"}
    assert not preferred_review_available(scope, "missing", {"key": "TESTWP-1"})


def _verify_scope(current_account, preferred_ready):
    ticket = {"key": "TESTWP-1", "project": "TESTWP", "labels": [], "jira_created": None}
    account = SimpleNamespace(machines=["node-a"])
    dev = {"status": "finished", "outcome": "done", "account_id": "developer",
           "pr_url": "https://github.test/pr/1", "details": {"local_gate": {"status": "green"}}}

    def one(query, *_args):
        return dev if "step='dev'" in query else {"account_id": "reviewer-one"}

    scope = SimpleNamespace(
        roles={"verify"}, backoff_rows=lambda _step: [], held=set(),
        ctx=SimpleNamespace(db=SimpleNamespace(all=lambda *_args: [ticket], one=one)),
        deps=SimpleNamespace(
            last_dev=lambda *_args, **_kwargs: {"account_id": "developer", "pr_url": "https://github.test/pr/1"},
            eligible=lambda *_args, **_kwargs: ([{"key": ticket["key"], "step": "verify"}]
                                               if preferred_ready else [])),
        account=SimpleNamespace(id=current_account),
        repo=SimpleNamespace(account=lambda _key: account),
        scoped_key="", scoped_ticket=(), projects=["TESTWP"],
        needs_missing_container_runtime=lambda *_args: False,
        explicit_only_blocks=lambda _ticket: False, allowed=lambda *_args: True,
        probation=lambda _step: False,
    )
    return scope


def test_verification_prefers_first_reviewer_only_while_that_account_is_eligible():
    assert verify_candidates(_verify_scope("reviewer-two", True)) == []
    assert [row["key"] for row in verify_candidates(_verify_scope("reviewer-two", False))] == ["TESTWP-1"]
    assert [row["key"] for row in verify_candidates(_verify_scope("reviewer-one", True))] == ["TESTWP-1"]


def test_verify_candidate_ready_keeps_the_existing_runtime_and_author_gates():
    scope = _verify_scope("reviewer-two", True)
    ticket = {"key": "TESTWP-1", "project": "TESTWP", "labels": []}

    assert _verify_candidate_ready(scope, ticket, set())
    scope.account.id = "developer"
    assert not _verify_candidate_ready(scope, ticket, set())


def test_verify_claim_carries_prior_findings_round_count_and_reviewer():
    runs = [{"account_id": "codex-01", "outcome": "rework", "details": {
        "verdict": "FAIL", "review_round": 1,
        "review_findings": [{"id": "F1", "wording": "retry loses token", "status": "new", "first_round": 1}],
    }}]
    work = SimpleNamespace(
        prior_dev={"pr_url": "https://github.test/org/repo/pull/7", "branch": "fix-retry", "head_sha": "abc",
                   "account_id": "claude-01", "summary": "Fixed retry", "details": {}},
        ticket={"key": "TESTWP-1"}, store=SimpleNamespace(completed_verify_runs=lambda _key: runs),
    )

    pr = verify_extras(work)["pr"]

    assert pr["review_history"][0]["wording"] == "retry loses token"
    assert pr["review_round"] == 2 and pr["rework_returns"] == 1


def test_third_would_be_return_goes_to_intake_planning(monkeypatch):
    calls = []
    monkeypatch.setattr(verify, "note", lambda *_args, **_kwargs: "planning note")
    monkeypatch.setattr(verify, "api_node", lambda _api: "node-a")
    jira = lambda action, **body: calls.append((action, body))
    claim = SimpleNamespace(pr={"rework_returns": 2})
    result = {}

    actual = verify._rework(claim, object(), jira, lambda body: calls.append(("pr", body)), result,
                            "- 1. F1: repeated blocker", "verify failed")

    assert actual["outcome"] == "split_plan" and actual["details"]["planning_required"] == "JAIF-335"
    assert [call[0] for call in calls] == ["pr", "comment", "fields", "transition"]
    assert calls[2][1]["add_labels"] == ["needs-split"]
    assert calls[2][1]["remove_labels"] == ["rework", "aiflow-ready"]
    assert calls[3][1]["to"] == "Intake"


def test_first_two_returns_keep_the_existing_rework_side_effect_order(monkeypatch):
    calls = []
    monkeypatch.setattr(verify, "note", lambda *_args, **_kwargs: "rework note")
    monkeypatch.setattr(verify, "api_node", lambda _api: "node-a")
    jira = lambda action, **body: calls.append((action, body))
    claim = SimpleNamespace(pr={"rework_returns": 1})
    result = {}

    actual = verify._rework(claim, object(), jira, lambda body: calls.append(("pr", body)), result,
                            "- 1. F1: retry fails", "verify failed")

    assert actual["outcome"] == "rework"
    assert [call[0] for call in calls] == ["pr", "comment", "fields", "transition"]
    assert calls[2][1] == {"add_labels": ["rework"], "remove_labels": ["aiflow-ready"]}
    assert calls[3][1]["to"] == "To Do"


def test_intake_recognizes_the_explicit_split_planning_label():
    assert intake._ticket_needs_split({"labels": ["needs-split"]})
    assert "needs-split" in intake.KNOWN_FIXED_LABELS


def test_split_parent_is_not_offered_for_another_intake_run():
    query = []
    parent = {"key": "TESTWP-1", "project": "TESTWP", "labels": ["aiflow-split-parent"]}
    db = SimpleNamespace(all=lambda sql, _params: query.append(sql) or
                          ([] if "aiflow-split-parent" in sql else [parent]))
    scope = SimpleNamespace(
        roles={"intake_refiner"}, intake_backoff_rows=lambda _step: [],
        ctx=SimpleNamespace(db=db), projects=["TESTWP"], scoped_key="", scoped_ticket=(), held=set(),
        needs_missing_container_runtime=lambda *_args: False, explicit_only_blocks=lambda _ticket: False,
        allowed=lambda *_args: True, probation=lambda _step: False,
    )

    assert intake_candidates(scope) == []
    assert "NOT ('aiflow-split-parent' = ANY(labels))" in query[0]


def test_review_metrics_show_rounds_and_later_round_new_findings():
    rows = [
        {"ticket_key": "TESTWP-1", "details": {"review_round": 1, "review_findings": [
            {"id": "F1", "status": "new"}, {"id": "F2", "status": "new"}]}},
        {"ticket_key": "TESTWP-1", "details": {"review_round": 2, "review_findings": [
            {"id": "F1", "status": "fixed"}, {"id": "F2", "status": "not_fixed"},
            {"id": "F3", "status": "new"}]}},
        {"ticket_key": "TESTWP-2", "details": {"review_round": 1, "review_findings": []}},
    ]

    result = review_round_metrics.calculate_review_round_metrics([
        review_round_models.ReviewRun(row["ticket_key"], row["details"]) for row in rows])

    assert result["target_rounds"] == 2 and result["merged_tickets"] == 2
    assert result["average_rounds"] == 1.5
    assert result["tickets"][0] == {"key": "TESTWP-1", "rounds": 2, "reviews": [
        {"round": 1, "findings": 2, "new_findings": 0},
        {"round": 2, "findings": 2, "new_findings": 1}]}


def test_review_count_helper_excludes_resolved_findings_and_later_round_newness():
    details = {"review_findings": [{"id": "F1", "status": "fixed"},
                                   {"id": "F2", "status": "not_fixed"},
                                   {"id": "F3", "status": "new"}]}

    assert review_round_metrics._review_counts(details, 2) == {
        "round": 2, "findings": 2, "new_findings": 1}


def test_review_metrics_query_is_scoped_and_empty_metrics_have_no_average():
    class Database:
        def __init__(self):
            self.calls = []

        def all(self, query, params):
            self.calls.append((query, params))
            return []

    db = Database()

    result = review_round_metrics.review_round_metrics(review_round_repository.ReviewRoundsRepository(db),
                                                       ["TESTWP"])

    assert db.calls[0][1] == (["TESTWP"],)
    assert "t.project=ANY(%s)" in db.calls[0][0] and "t.pr_state='merged'" in db.calls[0][0]
    assert result == {"target_rounds": 2, "merged_tickets": 0, "average_rounds": None, "tickets": []}


def test_non_reviews_do_not_count_toward_rounds_or_reviewer_affinity():
    runs = [
        {"account_id": "reviewer-one", "outcome": "rework", "details": {
            "verdict": "FAIL", "review_findings": [{"id": "F1", "wording": "blocker", "status": "new"}]}},
        {"account_id": "blocked-reviewer", "outcome": "failed", "details": {
            "verdict": "BLOCKED", "review_findings": []}},
        {"account_id": "quota-reviewer", "outcome": "quota", "details": {"review_findings": []}},
    ]

    assert review_findings.review_rounds_completed(runs) == 1
    assert review_findings.first_reviewer(runs) == "reviewer-one"
    assert review_findings.history_from_runs(runs)[0]["wording"] == "blocker"


def test_generated_finding_ids_skip_ids_already_named_by_the_reviewer():
    previous = [{"id": "F1", "wording": "original", "status": "new", "first_round": 1}]
    text = "REVIEW FINDINGS:\n- F1 [NOT FIXED]\n- F2 NEW: reviewer-supplied ID\n- NEW: generated ID\n"

    history, active, _count = review_findings.update_review_findings(text, previous, 2)

    assert [item["id"] for item in history] == ["F1", "F2", "F3"]
    assert "- 3. F3: generated ID" in active


def test_dashboard_review_rounds_route_reports_merged_ticket_findings(client, ctx):
    add_ticket(ctx.db, "TESTWP-351", status="Done")
    ctx.db.run("UPDATE tickets SET pr_state='merged' WHERE key=%s", ("TESTWP-351",))
    rows = [
        {"verdict": "FAIL", "review_round": 1, "review_findings": [
            {"id": "F1", "wording": "retry fails", "status": "new"}]},
        {"verdict": "PASS", "review_round": 2, "review_findings": [{"id": "F1", "wording": "retry fails", "status": "fixed"},
                              {"id": "F2", "wording": "empty fails", "status": "new"}]},
    ]
    for index, details in enumerate(rows):
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,details)"
                   " VALUES (%s,%s,'TESTWP','verify','codex-01','codex','node1',1,'finished',%s,%s)",
                   (str(uuid.uuid4()), "TESTWP-351", "rework" if index == 0 else "merged", J(details)))

    response = client.get("/v1/dashboard/review-rounds")

    assert response.status_code == 200
    assert response.json() == {"target_rounds": 2, "merged_tickets": 1, "average_rounds": 2.0,
                               "tickets": [{"key": "TESTWP-351", "rounds": 2, "reviews": [
                                   {"round": 1, "findings": 1, "new_findings": 0},
                                   {"round": 2, "findings": 1, "new_findings": 1}]}]}
