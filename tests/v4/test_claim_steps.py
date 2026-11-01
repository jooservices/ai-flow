"""Focused tests for the named work-claim stages and response builders."""
from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aiflow.v4.control.features.work import claim as claim_module
from aiflow.v4.control.features.work import claim_extras, claim_steps
from aiflow.v4.control.features.work.repository import WorkRepository
from aiflow.v4.control.features.work_routes.bodies import ClaimBody


def _ticket(key: str = "TESTWP-CLAIM") -> dict:
    return {"key": key, "project": "TESTWP", "step": "dev", "summary": f"Summary of {key}",
            "description": "Acceptance criteria", "labels": ["aiflow-ready"], "priority": "Medium",
            "type": "Task", "probation": False}


def _body(key: str = "TESTWP-CLAIM", step: str = "dev") -> ClaimBody:
    return ClaimBody(ticket=key, account="claude-01", step=step)


def _deps(ticket: dict | None = None):
    selected = ticket or _ticket()
    return SimpleNamespace(
        eligible=lambda *_args, **_kwargs: [selected], master_candidate=lambda *_args: selected,
        releases=lambda: None, ticket_epic=lambda _ticket: "", capacity_history=lambda: [],
        related_hits=lambda _ticket: [], bot_identity=lambda: ("AI Flow", "aiflow[bot]"))


def _work(ctx, ticket: dict | None = None, *, step: str = "dev", deps=None, store=None):
    selected = ticket or _ticket()
    return claim_steps.Claim(ctx=ctx, deps=deps or _deps(selected), body=_body(selected["key"], step), node="cc",
                             store=store if store is not None else WorkRepository(ctx.db))


@dataclass
class _BoardRow:
    key: str
    type: str
    status: str
    summary: str
    parent_key: str | None


def test_early_refusal_for_live_ticket_lease_keeps_the_account_check_first(ctx, monkeypatch):
    work = _work(ctx)
    monkeypatch.setattr(ctx.leases, "holder", lambda _ticket: "another-run")
    monkeypatch.setattr(claim_steps.load_policy_api, "work_claim_capacity",
                        lambda *_args: pytest.fail("load must not be checked for a live ticket lease"))

    refusal = claim_steps.refuse_early(work)

    assert refusal.status_code == 409
    assert refusal.body == b'{"claimed":false,"reason":"ticket is already claimed"}'


def test_early_refusal_for_licence_preserves_its_safe_reason(ctx, monkeypatch):
    work = _work(ctx)
    monkeypatch.setattr(ctx.leases, "holder", lambda _ticket: None)
    monkeypatch.setattr(claim_steps.load_policy_api, "work_claim_capacity", lambda *_args: ({}, None))

    def refuse(*_args, **_kwargs):
        raise claim_steps.licensing.LicenseError("plan denied")

    monkeypatch.setattr(claim_steps.licensing, "enforce_work_entitlement", refuse)
    refusal = claim_steps.refuse_early(work)

    assert refusal.status_code == 409 and json.loads(refusal.body) == {"claimed": False, "reason": "plan denied"}


def test_early_refusal_for_version_skew_follows_licence_check(ctx, monkeypatch):
    work = _work(ctx)
    monkeypatch.setattr(ctx.leases, "holder", lambda _ticket: None)
    monkeypatch.setattr(claim_steps.load_policy_api, "work_claim_capacity", lambda *_args: ({}, None))
    monkeypatch.setattr(claim_steps.licensing, "enforce_work_entitlement", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(claim_steps.rollout, "node_compatible",
                        lambda *_args: {"compatible": False, "reason": "old release"})

    refusal = claim_steps.refuse_early(work)

    assert refusal.status_code == 409
    assert json.loads(refusal.body) == {"claimed": False, "reason": "version skew: old release"}


def test_select_ticket_applies_entitlement_and_owner_authorization(ctx, monkeypatch):
    ticket = _ticket()
    work = _work(ctx, ticket)
    work.state = {"license": "ok"}
    owner_answer = {"answer": "Use option A", "question": "Which option?"}
    monkeypatch.setattr(claim_steps.licensing, "enforce_project_entitlement", lambda *_a, **_kw: None)
    monkeypatch.setattr(claim_steps.triage_claim_service, "development_eligible", lambda *_a: True)
    monkeypatch.setattr(claim_steps.triage_claim_service, "authorize_development",
                        lambda *_a: (True, owner_answer))

    refusal = claim_steps.select_ticket(work)

    assert refusal is None
    assert work.ticket == ticket and work.owner_answer == owner_answer
    assert work.account.id == "claude-01" and work.run_id


def test_select_ticket_returns_the_existing_not_eligible_refusal(ctx, monkeypatch):
    work = _work(ctx, deps=_deps(None))
    work.deps.eligible = lambda *_args, **_kwargs: []
    monkeypatch.setattr(claim_steps.licensing, "enforce_project_entitlement", lambda *_a, **_kw: None)

    refusal = claim_steps.select_ticket(work)

    assert refusal.status_code == 409 and json.loads(refusal.body) == {"claimed": False, "reason": "not eligible"}


def test_probation_assignment_refusal_and_success(ctx, monkeypatch):
    work = _work(ctx, {**_ticket(), "probation": True})
    work.ticket = {**_ticket(), "probation": True}
    work.account = SimpleNamespace(id="claude-01")
    assign = []
    monkeypatch.setattr(claim_steps.role_health, "role_of", lambda _step: "dev")
    monkeypatch.setattr(claim_steps.role_health, "assign_probation",
                        lambda *_args: assign.pop(0) if assign else None)

    refusal = claim_steps.acquire_probation(work)
    assign.append("probation-1")
    accepted = claim_steps.acquire_probation(work)

    assert refusal.status_code == 409
    assert json.loads(refusal.body) == {"claimed": False, "reason": "a probation ticket is already running"}
    assert accepted is None and work.probation_id == "probation-1"


def test_lease_refusal_drops_the_probation_assignment(ctx, monkeypatch):
    work = _work(ctx)
    work.ticket = _ticket()
    work.run_id = "run-1"
    work.probation_id = "probation-1"
    monkeypatch.setattr(ctx.leases, "claim", lambda *_args: SimpleNamespace(ok=False, holder="run-other"))
    dropped = []
    monkeypatch.setattr(claim_steps.role_health, "drop_probation", lambda *_args: dropped.append(_args[1]))

    refusal = claim_steps.acquire_lease(work)

    assert refusal.status_code == 409 and json.loads(refusal.body) == {"claimed": False, "reason": "held by run-other"}
    assert dropped == ["probation-1"]


def test_hot_file_refusal_releases_lease_and_drops_probation(ctx, monkeypatch):
    work = _work(ctx)
    work.ticket = _ticket()
    work.run_id = "run-1"
    work.probation_id = "probation-1"
    work.lease = SimpleNamespace(fence=4)
    released, dropped = [], []
    monkeypatch.setattr(ctx.leases, "release", lambda *args: released.append(args))
    monkeypatch.setattr(claim_steps.hotfiles, "predict", lambda *_args: {"hot": ["app.py"]})
    monkeypatch.setattr(claim_steps.hotfiles, "reserve", lambda *_args, **_kw: ("run-other", ["app.py"]))
    monkeypatch.setattr(claim_steps.role_health, "drop_probation", lambda *_args: dropped.append(_args[1]))

    refusal = claim_steps.reserve_hot_files(work)

    assert refusal.status_code == 409
    assert json.loads(refusal.body) == {"claimed": False, "reason": "hot file app.py is reserved by run-other"}
    assert released == [("TESTWP-CLAIM", "run-1", 4)] and dropped == ["probation-1"]


def test_repository_settings_captures_the_project_and_ticket(ctx, monkeypatch):
    work = _work(ctx)
    work.ticket = _ticket()
    work.repo = ctx.repo()
    work.account = work.repo.account("claude-01")
    monkeypatch.setattr(claim_steps.dev_history, "delivered_or_last", lambda *_args: None)

    claim_steps.repository_settings(work)

    assert work.model == "opus" and work.run_token
    assert work.ticket_data["key"] == "TESTWP-CLAIM" and work.ticket_data["epic"] == ""
    assert work.selected_repo["repo"] == "jooservices/test-aiflow-wp"
    assert work.run_details["repository"] == "jooservices/test-aiflow-wp"


@pytest.mark.parametrize(
    ("known", "fits", "expected"),
    [(True, True, "Assigned to claude-01 (claude, senior): estimated work is 100–200 tokens and "
                  "5–10 minutes; quota permits this run; tier preference for this ticket is middle, "
                  "then senior, then junior."),
     (True, False, "Assigned to claude-01 (claude, senior) because it remains eligible and no quota-safe "
                   "eligible account was identified. The estimate is 100–200 tokens and 5–10 minutes; "
                   "quota permits this run."),
     (False, False, "Assigned to claude-01 (claude, senior) by the existing eligibility rules. The estimate "
                    "is 100–200 tokens and 5–10 minutes; quota history is not reliable enough to compare "
                    "eligible accounts.")],
)
def test_dev_assignment_preserves_each_reason_sentence(ctx, monkeypatch, known, fits, expected):
    work = _work(ctx, store=SimpleNamespace(account_forecast=lambda _account: None))
    work.ticket = _ticket()
    work.account = SimpleNamespace(id="claude-01", provider="claude", tier="senior", sessions=2)
    work.run_details = {}
    estimate = {"tokens": {"low": 100, "high": 200}, "time_minutes": {"low": 5, "high": 10}, "size": "M"}
    fit = {"known": known, "fits": fits, "left_tokens": 100, "reason": "quota permits this run"}
    monkeypatch.setattr(claim_steps.capacity, "estimate", lambda *_args: estimate)
    monkeypatch.setattr(claim_steps.capacity, "with_active_quota_reservations", lambda _db, forecasts: forecasts)
    monkeypatch.setattr(claim_steps.capacity, "account_fit", lambda *_args: fit)
    work.deps.capacity_history = lambda: []

    claim_steps.dev_assignment(work)

    reason = work.run_details["capacity"]["assignment_reason"]
    assert expected == reason
    assert work.quota_reservation_tokens == 200


def test_run_registration_refusal_releases_lease_and_unreserves_hot_files(ctx, monkeypatch):
    work = _work(ctx)
    work.ticket = _ticket()
    work.account = SimpleNamespace(id="claude-01", provider="claude", sessions=2)
    work.run_id, work.run_token, work.model, work.node = "run-1", "token", "opus", "cc"
    work.lease = SimpleNamespace(fence=4)
    work.load_capacity, work.run_details = {}, {}
    released, unreserved = [], []
    monkeypatch.setattr(ctx.leases, "release", lambda *args: released.append(args))
    monkeypatch.setattr(claim_steps.load_policy_api, "claim_run", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(claim_steps.hotfiles, "unreserve", lambda _db, run_id: unreserved.append(run_id))

    refusal = claim_steps.register_run(work)

    assert refusal.status_code == 409
    assert json.loads(refusal.body) == {
        "claimed": False,
        "reason": "account claude-01 is at its limit of 2 concurrent sessions or machine load capacity is full",
    }
    assert released == [("TESTWP-CLAIM", "run-1", 4)] and unreserved == ["run-1"]


def test_record_claim_keeps_the_run_side_effect_order(monkeypatch):
    calls = []
    account = SimpleNamespace(id="claude-01", provider="claude")
    ticket = _ticket()
    store = SimpleNamespace(
        set_run_details=lambda *_args: calls.append("details"),
        inherit_pull_request=lambda *_args: calls.append("inherited"),
        mark_account_working=lambda *_args: calls.append("working"))
    ctx = SimpleNamespace(
        db=SimpleNamespace(transaction=nullcontext),
        provider_eligibility=SimpleNamespace(record_assignment=lambda *_args: calls.append("assignment")),
        events=SimpleNamespace(emit=lambda *_args, **_kwargs: calls.append("probation")))
    work = claim_steps.Claim(ctx=ctx, deps=None, body=_body(), node="cc", store=store, ticket=ticket,
                             account=account, run_id="run-1", lease=SimpleNamespace(fence=4), model="opus",
                             run_details={"repository": "repo"})
    monkeypatch.setattr(claim_steps, "publish_for", lambda *_args: calls.append("event"))

    claim_steps.record_claim(work)

    assert calls == ["assignment", "details", "working", "event"]


def test_each_step_extras_builder_returns_its_step_payload(monkeypatch):
    ticket = _ticket()
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = SimpleNamespace(
        node_tools=lambda _node: SimpleNamespace(as_row=lambda: {"tools": []}),
        latest_rework=lambda _key: None, latest_answered_triage=lambda _key: None,
        dev_finished_since=lambda *_args: False, blocked_question_before=lambda *_args: "",
        open_project_tickets=lambda *_args: [_BoardRow("TESTWP-OTHER", "Task", "To Do", "Other", None)])
    ctx = SimpleNamespace(db=object(), jira=SimpleNamespace(recent_comments=lambda *_a, **_kw: ["comment"]))
    work = claim_steps.Claim(ctx=ctx, deps=SimpleNamespace(related_hits=lambda _ticket: ["hit"]),
                             body=_body(), node="cc", store=store, ticket=ticket, prior_dev={
                                 "started_at": timestamp, "branch": "feature/x", "pr_url": "https://example/pull/7",
                                 "head_sha": "abc", "account_id": "codex-01", "summary": "Done", "details": {}})
    monkeypatch.setattr(claim_extras.tooling, "resolve_ticket", lambda *_args: None)
    monkeypatch.setattr(claim_extras.tooling, "machine_tools", lambda _row: ["php"])
    monkeypatch.setattr(claim_extras.escalations, "pending_answer", lambda *_args: None)
    monkeypatch.setattr(claim_extras.environment_reports, "pending_post_merge", lambda *_args: [])

    assert claim_extras.dev_extras(work)["machine_tools"] == ["php"]
    assert claim_extras.verify_extras(work)["pr"]["number"] == 7
    work.triage_context = {"question": "Which?"}
    assert claim_extras.triage_extras(work) == {"question": "Which?"}
    assert claim_extras.post_merge_extras(work) == {"post_merge_criteria": []}
    assert claim_extras.intake_extras(work) == {"recent_comments": ["comment"]}
    assert claim_extras.impact_extras(work)["project_tickets"][0]["key"] == "TESTWP-OTHER"
    assert claim_extras.master_extras(work) == {"master_scope": "global"}
    assert claim_extras.related_extras(work) == {"related": ["hit"]}


def test_context_pack_adds_search_results_and_uses_the_failure_fallback(monkeypatch):
    work = claim_steps.Claim(ctx=SimpleNamespace(db=object()), deps=None, body=_body(), node="cc", store=None,
                             ticket=_ticket(), run_id="run-1", epic="EPIC-1")
    monkeypatch.setattr(claim_extras.knowledge, "context_pack", lambda *_args, **_kwargs: {"items": ["x"]})
    extra = {}
    claim_extras.context_pack(work, extra)
    assert extra["context_pack"] == {"items": ["x"]}

    def fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(claim_extras.knowledge, "context_pack", fail)
    claim_extras.context_pack(work, extra)
    assert extra["context_pack"] == {"project": "TESTWP", "ticket": "TESTWP-CLAIM", "items": []}


def test_final_response_builder_preserves_success_fields(monkeypatch):
    ticket = _ticket()
    account = SimpleNamespace(id="claude-01", provider="claude")
    work = claim_steps.Claim(
        ctx=SimpleNamespace(db=object(), settings=SimpleNamespace(lease_ms=90_000)),
        deps=SimpleNamespace(bot_identity=lambda: ("AI Flow", "aiflow[bot]")), body=_body(), node="cc", store=None,
        ticket=ticket, ticket_data={"key": ticket["key"]}, account=account, project=None, selected_repo={},
        lease=SimpleNamespace(fence=4), run_id="run-1", run_token="token", model="opus", effort="high")
    monkeypatch.setattr(claim_extras.claim_project, "payload", lambda *_args: {"repo": "test"})

    result = claim_extras.final_response(work, {"related": []})

    assert result == {
        "related": [], "claimed": True, "step": "dev", "run_id": "run-1", "fence": 4,
        "run_token": "token", "lease_ms": 90_000, "model": "opus", "effort": "high",
        "provider": "claude", "account": "claude-01", "identity": ("AI Flow", "aiflow[bot]"),
        "ticket": {"key": "TESTWP-CLAIM"}, "project": {"repo": "test"}}


def test_claim_response_matches_the_same_steps_run_by_hand(client, ctx, monkeypatch):
    ticket = _ticket("TESTWP-SAME")
    body = _body(ticket["key"])
    deps = _deps(ticket)
    fixed_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(claim_steps.uuid, "uuid4", lambda: fixed_id)
    monkeypatch.setattr(claim_steps.secrets, "token_urlsafe", lambda _size: "fixed-token")
    monkeypatch.setattr(ctx.leases, "holder", lambda _ticket: None)
    monkeypatch.setattr(ctx.leases, "claim", lambda *_args: SimpleNamespace(ok=True, fence=1))
    monkeypatch.setattr(ctx.leases, "release", lambda *_args: True)
    monkeypatch.setattr(claim_steps.load_policy_api, "work_claim_capacity", lambda *_args: ({}, None))
    monkeypatch.setattr(claim_steps.load_policy_api, "claim_run", lambda *_args, **_kw: True)
    monkeypatch.setattr(claim_steps.licensing, "enforce_work_entitlement", lambda *_args, **_kw: {})
    monkeypatch.setattr(claim_steps.licensing, "enforce_project_entitlement", lambda *_args, **_kw: None)
    monkeypatch.setattr(claim_steps.rollout, "node_compatible", lambda *_args: {"compatible": True, "reason": ""})
    monkeypatch.setattr(claim_steps.triage_claim_service, "development_eligible", lambda *_args: True)
    monkeypatch.setattr(claim_steps.triage_claim_service, "authorize_development", lambda *_args: (True, None))
    monkeypatch.setattr(claim_steps.hotfiles, "predict", lambda *_args: {"hot": []})
    monkeypatch.setattr(claim_steps.hotfiles, "reserve", lambda *_args, **_kw: None)
    monkeypatch.setattr(claim_steps.capacity, "estimate", lambda *_args: {
        "tokens": {"low": 10, "high": 20}, "time_minutes": {"low": 1, "high": 2}, "size": "S"})
    monkeypatch.setattr(claim_steps.capacity, "with_active_quota_reservations", lambda _db, values: values)
    monkeypatch.setattr(claim_steps.capacity, "account_fit", lambda *_args: {
        "known": False, "fits": False, "left_tokens": None, "reason": "unknown"})
    monkeypatch.setattr(claim_extras.tooling, "resolve_ticket", lambda *_args: None)
    monkeypatch.setattr(claim_extras.tooling, "machine_tools", lambda _row: [])
    monkeypatch.setattr(claim_extras.knowledge, "context_pack", lambda *_args, **_kw: {"items": []})
    monkeypatch.setattr(claim_extras.claim_project, "payload", lambda *_args: {"repo": "test"})
    monkeypatch.setattr(claim_steps, "publish_for", lambda *_args: None)
    monkeypatch.setattr(ctx.provider_eligibility, "record_assignment", lambda *_args: None)

    routed = claim_module.claim(ctx, deps, body, "cc")
    work = claim_steps.Claim(ctx=ctx, deps=deps, body=body, node="cc", store=WorkRepository(ctx.db))
    for step in (claim_steps.refuse_early, claim_steps.select_ticket, claim_steps.acquire_probation,
                 claim_steps.acquire_lease, claim_steps.reserve_hot_files):
        assert step(work) is None
    claim_steps.repository_settings(work)
    claim_steps.dev_assignment(work)
    assert claim_steps.register_run(work) is None
    claim_steps.record_claim(work)
    extra = claim_extras.dev_extras(work)
    extra.update(claim_extras.related_extras(work))
    claim_extras.context_pack(work, extra)
    manual = claim_extras.final_response(work, extra)

    assert routed == manual
