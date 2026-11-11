"""A missing verification verdict retries elsewhere before it affects account health."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import load_policy, role_health, rollout
from aiflow.v4.control.db import J
from aiflow.v4.control.features.work.eligibility_candidates import (
    _verify_account_barred, _verify_candidate, _verify_retry_blocked,
    _verify_retry_peer_account, verify_candidates,
)
from aiflow.v4.control.repositories.runs import (
    VERIFY_TICKET_HISTORY_SQL, VerifyTicketHistory, verify_retry_backoff, verify_ticket_history,
)
from aiflow.v4.worker import verify

PR = "https://github.com/jooservices/test-aiflow-wp/pull/7"


def _join(client, name):
    joined = join_node(client, name)
    assert joined.status_code == 200
    return {"Authorization": f"Bearer {joined.json()['node_token']}"}


def _insert_run(ctx, key, step, account, outcome, *, details=None, minutes_ago=10, pr_url="", summary=""):
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    provider = account.split("-", 1)[0]
    node = "node1" if account == "codex-01" else "cc"
    ctx.db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
        "started_at,ended_at,pr_url,summary,details) "
        "VALUES (%s,%s,'TESTWP',%s,%s,%s,'m',%s,1,'finished',%s,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), key, step, account, provider, node, outcome, started,
         started + timedelta(minutes=1), pr_url, summary, J(details or {})),
    )


def _in_verify(ctx, key="TESTWP-50"):
    add_ticket(ctx.db, key, "In Verify")
    _insert_run(ctx, key, "dev", "opencode-01", "done", pr_url=PR, minutes_ago=60,
                details={"local_gate": {"status": "green"}})


def _no_verdict(ctx, key, account, minutes_ago):
    _insert_run(ctx, key, "verify", account, "failed", details={"no_verdict": True},
                minutes_ago=minutes_ago, summary="no verdict (missing VERDICT line): x")


def _offered(client, headers, account):
    response = client.get("/v1/work/candidates", params={"account": account}, headers=headers)
    assert response.status_code == 200
    return {(ticket["key"], ticket["step"]) for ticket in response.json()["tickets"]}


def test_no_verdict_run_goes_to_another_account_at_once(client, ctx):
    cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    assert ("TESTWP-50", "verify") in _offered(client, cc, "claude-01")
    assert ("TESTWP-50", "verify") in _offered(client, node1, "codex-01")

    _no_verdict(ctx, "TESTWP-50", "codex-01", 5)

    assert ("TESTWP-50", "verify") in _offered(client, cc, "claude-01")
    assert ("TESTWP-50", "verify") not in _offered(client, node1, "codex-01")

    _no_verdict(ctx, "TESTWP-50", "claude-01", 4)
    assert ("TESTWP-50", "verify") in _offered(client, node1, "codex-01")

    _no_verdict(ctx, "TESTWP-50", "codex-01", 3)
    assert ("TESTWP-50", "verify") not in _offered(client, node1, "codex-01")
    assert ("TESTWP-50", "verify") in _offered(client, cc, "claude-01")


def test_prior_no_verdict_from_before_new_development_round_is_ignored(client, ctx):
    cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 55)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 50)
    assert ("TESTWP-50", "verify") not in _offered(client, node1, "codex-01")

    _insert_run(ctx, "TESTWP-50", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=40,
                details={"local_gate": {"status": "green"}})

    assert ("TESTWP-50", "verify") in _offered(client, node1, "codex-01")
    assert ("TESTWP-50", "verify") in _offered(client, cc, "claude-01")
    assert verify_ticket_history(ctx.db, ["TESTWP-50"]) == {}


def test_previous_verifier_stays_behind_an_untried_peer_after_fifteen_minutes(client, ctx):
    cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    _insert_run(ctx, "TESTWP-50", "verify", "claude-01", "done", details={"verdict": "PASS"}, minutes_ago=40)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 16)

    assert ("TESTWP-50", "verify") not in _offered(client, node1, "codex-01")
    assert ("TESTWP-50", "verify") in _offered(client, cc, "claude-01")


def test_no_verdict_counts_against_the_verify_role_only_from_the_second_in_a_row(ctx):
    _in_verify(ctx)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 6)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 3)
    runs = ctx.db.all(role_health.RUNS_SQL, {"steps": ["verify"], "account": "codex-01", "limit": 10})
    first, second = sorted(runs, key=lambda run: run["started_at"])
    assert role_health.classify(first).failure is None
    assert role_health.classify(second).failure == 1.0

    _insert_run(ctx, "TESTWP-50", "verify", "codex-01", "rework", details={"verdict": "FAIL"}, minutes_ago=2)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 1)
    latest = max(ctx.db.all(role_health.RUNS_SQL, {"steps": ["verify"], "account": "codex-01", "limit": 10}),
                 key=lambda run: run["started_at"])
    assert role_health.classify(latest).failure is None


def test_no_verdict_keeps_the_existing_failed_result_wording():
    response = SimpleNamespace(error_kind="", error_message="", final_text="provider output")
    review = verify.VerifyReview(response, "provider output", "not reported", "", "", [])
    state = SimpleNamespace(claim=SimpleNamespace(project={}), result={"details": {}})

    result = verify.collect_verification(state, None, review)

    assert result["outcome"] == "failed"
    assert result["summary"] == "no verdict (missing VERDICT line): provider output"


def test_worker_marks_a_missing_verdict_for_control_retry_policy():
    response = SimpleNamespace(error_kind="", error_message="", final_text="provider output")
    review = verify.VerifyReview(response, "provider output", "not reported", "", "", [])
    state = SimpleNamespace(claim=SimpleNamespace(project={}), result={"details": {}})

    verify.collect_verification(state, None, review)

    assert state.result["details"]["no_verdict"] is True


def test_verify_retry_backoff_query_returns_ticket_keys_and_keeps_its_scope():
    class Database:
        def all(self, query, params):
            assert "details->>'no_verdict'" in query
            assert params == {"ticket_key": "TESTWP-50"}
            return [{"ticket_key": "TESTWP-50"}]

    assert verify_retry_backoff(Database(), "TESTWP-50") == {"TESTWP-50"}


def test_verify_backoff_restarts_after_a_coordinator_resolved_the_handoff(ctx):
    from aiflow.v4.control.features.incidents import repository as incident_repository

    add_ticket(ctx.db, "TESTWP-60", "In Verify")
    _insert_run(ctx, "TESTWP-60", "verify", "codex-01", "failed", minutes_ago=2)  # still inside the 2 minute cooldown
    assert verify_retry_backoff(ctx.db, "TESTWP-60") == {"TESTWP-60"}

    incident = incident_repository.upsert_incident(ctx.db, "TESTWP-60", "ticket stuck", "handoff")
    ctx.db.run("INSERT INTO ticket_handoffs (ticket_key,incident_id,state,resolved_at,resolved_by,resolution)"
               " VALUES (%s,%s,'resolved',now(),'owner','the cause was fixed')", ("TESTWP-60", incident))

    assert verify_retry_backoff(ctx.db, "TESTWP-60") == set()


def test_verify_ticket_history_groups_newest_first_markers_by_account():
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    class Database:
        def all(self, query, params):
            assert "ORDER BY started_at DESC" in query
            assert "ended_at" in query
            assert "step = 'dev' AND d.outcome = 'done'" in query
            assert params == (["TESTWP-50"],)
            return [
                {"ticket_key": "TESTWP-50", "account_id": "codex-01", "no_verdict": True,
                 "ended_at": ended_at},
                {"ticket_key": "TESTWP-50", "account_id": "claude-01", "no_verdict": False,
                 "ended_at": ended_at - timedelta(minutes=1)},
                {"ticket_key": "TESTWP-50", "account_id": "codex-01", "no_verdict": False,
                 "ended_at": ended_at - timedelta(minutes=2)},
            ]

    assert verify_ticket_history(Database(), ["TESTWP-50"]) == {
        "TESTWP-50": VerifyTicketHistory({"codex-01": [True, False], "claude-01": [False]}, ended_at)}
    assert "max(d.started_at)" in VERIFY_TICKET_HISTORY_SQL


def test_verify_account_barred_needs_two_consecutive_no_verdict_results():
    assert not _verify_account_barred([])
    assert not _verify_account_barred([True])
    assert not _verify_account_barred([True, False])
    assert not _verify_account_barred([False, True])
    assert _verify_account_barred([True, True])
    assert _verify_account_barred([True, True, False])


def test_verify_retry_blocks_a_tried_account_at_every_wait_boundary_with_an_untried_peer(monkeypatch):
    peer = SimpleNamespace(id="claude-01", paused=False, machines=["cc"], roles=["verify"])
    repository = SimpleNamespace(accounts=[peer])
    context = SimpleNamespace(repo=lambda: repository)
    dependencies = SimpleNamespace(
        releases=lambda: None,
        eligible=lambda account, node, **options: [{"key": "TESTWP-50", "step": "verify"}],
    )
    scope = SimpleNamespace(ctx=context, deps=dependencies, account=SimpleNamespace(id="codex-01"), ticket_key=None)
    now = datetime.now(timezone.utc)
    scope.now = lambda: now
    monkeypatch.setattr(load_policy, "claim_limits", lambda *_args: {"allowed": True})
    monkeypatch.setattr(rollout, "node_compatible", lambda *_args: {"compatible": True})

    history = VerifyTicketHistory({"codex-01": [True], "claude-01": [False]})
    assert _verify_retry_blocked(scope, {"key": "TESTWP-50"}, "opencode-01", history)
    for elapsed in (timedelta(minutes=14, seconds=59), timedelta(minutes=15), timedelta(minutes=15, seconds=1)):
        assert _verify_retry_blocked(
            scope, {"key": "TESTWP-50"}, "opencode-01",
            VerifyTicketHistory({"codex-01": [True]}, now - elapsed))
    assert _verify_retry_blocked(scope, {"key": "TESTWP-50"}, "opencode-01",
                                 VerifyTicketHistory({"codex-01": [True, True]}))
    assert not _verify_retry_blocked(scope, {"key": "TESTWP-50"}, "opencode-01",
                                     VerifyTicketHistory({"codex-01": []}))


@pytest.mark.parametrize("elapsed_seconds", [899, 900, 901])
@pytest.mark.parametrize("first_reviewer", ["tried", "untried"])
def test_candidates_keep_untried_peer_ahead_across_time_and_first_reviewer(
        client, ctx, elapsed_seconds, first_reviewer):
    cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    if first_reviewer == "tried":
        _insert_run(ctx, "TESTWP-50", "verify", "codex-01", "done", details={"verdict": "PASS"}, minutes_ago=40)
        _insert_run(ctx, "TESTWP-50", "verify", "claude-01", "done", details={"verdict": "PASS"}, minutes_ago=30)
    else:
        _insert_run(ctx, "TESTWP-50", "verify", "claude-01", "done", details={"verdict": "PASS"}, minutes_ago=40)
        _insert_run(ctx, "TESTWP-50", "verify", "codex-01", "done", details={"verdict": "PASS"}, minutes_ago=30)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 16)
    ctx.db.run("UPDATE runs SET ended_at=now()-(%s * interval '1 second') "
               "WHERE ticket_key='TESTWP-50' AND account_id='codex-01' AND details->>'no_verdict'='true'",
               (elapsed_seconds,))

    assert ("TESTWP-50", "verify") not in _offered(client, node1, "codex-01")
    assert ("TESTWP-50", "verify") in _offered(client, cc, "claude-01")


def test_claim_revalidates_newly_eligible_peer_before_lease_and_run(client, ctx, monkeypatch):
    cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    _insert_run(ctx, "TESTWP-50", "verify", "codex-01", "done", details={"verdict": "PASS"}, minutes_ago=20)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 1)
    peer_available = [False]
    monkeypatch.setattr("aiflow.v4.control.features.work.eligibility_candidates._peer_verify_tickets",
                        lambda _scope, _account, _node, _cache:
                        {"TESTWP-50"} if peer_available[0] else set())
    assert ("TESTWP-50", "verify") in _offered(client, node1, "codex-01")

    peer_available[0] = True
    assert ("TESTWP-50", "verify") not in _offered(client, node1, "codex-01")
    refused = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "codex-01", "step": "verify"},
                          headers=node1)
    assert refused.status_code == 409 and refused.json() == {"claimed": False, "reason": "not eligible"}
    assert ctx.leases.holder("TESTWP-50") is None
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE ticket_key='TESTWP-50' AND status='running'")["n"] == 0

    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "claude-01", "step": "verify"},
                          headers=cc)
    assert claimed.status_code == 200 and claimed.json()["claimed"]


def test_one_no_verdict_can_be_claimed_immediately_when_no_peer_is_eligible(client, ctx, monkeypatch):
    _cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 1)
    monkeypatch.setattr("aiflow.v4.control.features.work.eligibility_candidates._peer_verify_tickets",
                        lambda *_args: set())

    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "codex-01", "step": "verify"},
                          headers=node1)

    assert claimed.status_code == 200 and claimed.json()["claimed"]


@pytest.mark.parametrize("peer_rejection", ["load", "version"])
def test_untried_first_reviewer_rejected_by_peer_probe_does_not_hide_offer_or_claim(
        client, ctx, monkeypatch, peer_rejection):
    cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    _insert_run(ctx, "TESTWP-50", "verify", "claude-01", "done", details={"verdict": "PASS"}, minutes_ago=30)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 5)

    if peer_rejection == "load":
        original_claim_limits = load_policy.claim_limits

        def claim_limits(_ctx, node):
            limits = original_claim_limits(_ctx, node)
            return {**limits, "allowed": False} if node == "cc" else limits

        monkeypatch.setattr(load_policy, "claim_limits", claim_limits)
    else:
        monkeypatch.setattr(rollout, "node_compatible", lambda _ctx, node, _releases:
                            {"compatible": node != "cc", "reason": "test"})

    assert ("TESTWP-50", "verify") in _offered(client, node1, "codex-01")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "codex-01", "step": "verify"},
                          headers=node1)

    assert claimed.status_code == 200 and claimed.json()["claimed"]


@pytest.mark.parametrize("unrelated_result", ["no_verdict", "ordinary_failure"])
def test_scoped_claim_uses_real_peer_probe_and_keeps_other_ticket_history_independent(
        client, ctx, monkeypatch, unrelated_result):
    cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx, "TESTWP-50")
    _in_verify(ctx, "TESTWP-51")
    _no_verdict(ctx, "TESTWP-50", "codex-01", 5)
    if unrelated_result == "no_verdict":
        _no_verdict(ctx, "TESTWP-51", "claude-01", 3)
    else:
        _insert_run(ctx, "TESTWP-51", "verify", "claude-01", "failed", details={"verdict": "FAIL"},
                    minutes_ago=3)

    from aiflow.v4.control.features.work_routes import routes

    real_eligible = routes.eligible_tickets
    peer_calls = []

    def tracked_eligible(*args, **options):
        result = real_eligible(*args, **options)
        if args[2] == "claude-01" and options.get("verify_retry_guard") is False:
            peer_calls.append((options, {(item["key"], item["step"]) for item in result}))
        return result

    monkeypatch.setattr(routes, "eligible_tickets", tracked_eligible)
    refused = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "codex-01", "step": "verify"},
                          headers=node1)

    assert refused.status_code == 409 and refused.json() == {"claimed": False, "reason": "not eligible"}
    assert peer_calls == [({"routing": False, "capacity_routing": False, "ticket_key": "TESTWP-50",
                            "step_filter": "verify", "verify_retry_guard": False},
                           {("TESTWP-50", "verify")})]
    assert ("TESTWP-50", "verify") in _offered(client, cc, "claude-01")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "claude-01", "step": "verify"},
                          headers=cc)
    assert claimed.status_code == 200 and claimed.json()["claimed"]


def test_two_no_verdict_claim_bar_resets_after_a_successful_development_round(client, ctx):
    _cc, node1 = _join(client, "cc"), _join(client, "node1")
    _in_verify(ctx)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 6)
    _no_verdict(ctx, "TESTWP-50", "codex-01", 3)
    barred = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "codex-01", "step": "verify"},
                         headers=node1)
    assert barred.status_code == 409 and barred.json() == {"claimed": False, "reason": "not eligible"}
    assert ctx.leases.holder("TESTWP-50") is None

    _insert_run(ctx, "TESTWP-50", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=1,
                details={"local_gate": {"status": "green"}})
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "codex-01", "step": "verify"},
                          headers=node1)
    assert claimed.status_code == 200 and claimed.json()["claimed"]


def test_verify_candidate_builds_the_existing_offer_shape_without_services():
    ticket = {"key": "TESTWP-50", "summary": "Summary", "project": "TESTWP"}
    dependencies = SimpleNamespace()
    green_dev = {"status": "finished", "outcome": "done", "account_id": "opencode-01", "pr_url": PR,
                    "details": {"local_gate": {"status": "green"}}}
    scope = SimpleNamespace(
        needs_missing_container_runtime=lambda *_args: False, ctx=SimpleNamespace(db=SimpleNamespace(
            one=lambda *_args, **_kwargs: green_dev)),
        held=set(), deps=dependencies, account=SimpleNamespace(id="codex-01"),
        explicit_only_blocks=lambda _ticket: False, allowed=lambda *_args: True,
        verify_retry_guard=False, probation=lambda _role: False,
    )

    assert _verify_candidate(scope, ticket, set(), VerifyTicketHistory()) == {
        **ticket, "step": "verify", "probation": False}


def test_verify_retry_peer_must_have_no_prior_no_verdict_result():
    peer = SimpleNamespace(id="claude-01", paused=False, roles=["verify"])

    assert _verify_retry_peer_account(peer, "codex-01", "opencode-01", VerifyTicketHistory())
    assert not _verify_retry_peer_account(
        peer, "codex-01", "opencode-01", VerifyTicketHistory({"claude-01": [True]}))
    assert _verify_retry_peer_account(
        peer, "codex-01", "opencode-01", VerifyTicketHistory({"claude-01": [False]}))
    peer.roles = ["dev"]
    assert not _verify_retry_peer_account(peer, "codex-01", "opencode-01", VerifyTicketHistory())


@pytest.mark.parametrize("blocked_by", ["paused", "author", "role", "prior_no_verdict", "no_machine",
                                         "load", "version", "http_error", "unrelated_offer"])
def test_peer_probe_falls_back_when_no_untried_peer_is_eligible(monkeypatch, blocked_by):
    peer = SimpleNamespace(id="claude-01", paused=blocked_by == "paused",
                           machines=[] if blocked_by == "no_machine" else ["cc"],
                           roles=["dev"] if blocked_by == "role" else ["verify"])
    repository = SimpleNamespace(accounts=[peer])
    context = SimpleNamespace(repo=lambda: repository)

    def eligible(_account, _node, **_options):
        if blocked_by == "http_error":
            from fastapi import HTTPException
            raise HTTPException(503, "peer unavailable")
        if blocked_by == "unrelated_offer":
            return [{"key": "TESTWP-OTHER", "step": "dev"}]
        return [{"key": "TESTWP-50", "step": "verify"}]

    scope = SimpleNamespace(ctx=context, deps=SimpleNamespace(releases=lambda: None, eligible=eligible),
                            account=SimpleNamespace(id="codex-01"), ticket_key=None)
    monkeypatch.setattr(load_policy, "claim_limits", lambda *_args: {"allowed": blocked_by != "load"})
    monkeypatch.setattr(rollout, "node_compatible", lambda *_args:
                        {"compatible": blocked_by != "version"})
    history = VerifyTicketHistory({"codex-01": [True]})
    if blocked_by == "prior_no_verdict":
        history.by_account[peer.id] = [True]
    dev_account = peer.id if blocked_by == "author" else "opencode-01"

    assert not _verify_retry_blocked(scope, {"key": "TESTWP-50"}, dev_account, history)


def test_peer_probe_propagates_non_http_errors(monkeypatch):
    peer = SimpleNamespace(id="claude-01", paused=False, machines=["cc"], roles=["verify"])
    scope = SimpleNamespace(ctx=SimpleNamespace(repo=lambda: SimpleNamespace(accounts=[peer])),
                            deps=SimpleNamespace(releases=lambda: None,
                                                 eligible=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                                     RuntimeError("probe failed"))),
                            account=SimpleNamespace(id="codex-01"), ticket_key=None)
    monkeypatch.setattr(load_policy, "claim_limits", lambda *_args: {"allowed": True})
    monkeypatch.setattr(rollout, "node_compatible", lambda *_args: {"compatible": True})

    with pytest.raises(RuntimeError, match="probe failed"):
        _verify_retry_blocked(scope, {"key": "TESTWP-50"}, "opencode-01",
                              VerifyTicketHistory({"codex-01": [True]}))


def test_peer_eligibility_is_reused_across_tickets_in_one_poll(monkeypatch):
    now = datetime.now(timezone.utc)
    tickets = [{"key": "TESTWP-50", "project": "TESTWP", "labels": []},
               {"key": "TESTWP-51", "project": "TESTWP", "labels": []}]
    peer = SimpleNamespace(id="claude-01", paused=False, machines=["cc"], roles=["verify"])
    repository = SimpleNamespace(accounts=[peer])
    history_rows = [{"ticket_key": ticket["key"], "account_id": "codex-01", "no_verdict": True,
                     "ended_at": now - timedelta(minutes=1)} for ticket in tickets]
    queries = []

    class Database:
        def one(self, query, *_args):
            return {"status": "finished", "outcome": "done", "account_id": "opencode-01", "pr_url": PR,
                    "details": {"local_gate": {"status": "green"}}}

        def all(self, query, params):
            if "SELECT ticket_key, fails" in query:
                return []
            if "FROM tickets" in query:
                return tickets
            if "FROM runs" in query:
                return history_rows
            raise AssertionError(f"unexpected query: {query}")

    def eligible(account, node, **options):
        queries.append((account, node, options))
        return [{"key": ticket["key"], "step": "verify"} for ticket in tickets]

    scope = SimpleNamespace(
        roles={"verify"}, ctx=SimpleNamespace(db=Database(), repo=lambda: repository),
        deps=SimpleNamespace(last_dev=lambda *_args, **_kwargs: {"account_id": "opencode-01", "pr_url": PR},
                             releases=lambda: None, eligible=eligible),
        account=SimpleNamespace(id="codex-01"), repo=repository, ticket_key=None, projects=["TESTWP"],
        scoped_key="", scoped_ticket=(), verify_retry_guard=True, held=set(), now=lambda: now,
        needs_missing_container_runtime=lambda *_args: False,
        explicit_only_blocks=lambda _ticket: False, allowed=lambda *_args: True,
        probation=lambda _role: False,
    )
    monkeypatch.setattr(load_policy, "claim_limits", lambda *_args: {"allowed": True})
    monkeypatch.setattr(rollout, "node_compatible", lambda *_args: {"compatible": True})

    assert verify_candidates(scope) == []
    assert len(queries) == 1
    assert queries[0][2] == {"routing": False, "capacity_routing": False, "step_filter": "verify",
                             "verify_retry_guard": False}


def test_role_health_scores_no_verdict_from_the_previous_verify_result():
    run = {"step": "verify", "prev_no_verdict": False}

    assert role_health._score_no_verdict(run, {"no_verdict": True}).failure is None
    assert role_health._score_no_verdict({**run, "prev_no_verdict": True}, {"no_verdict": True}).failure == 1.0
    assert role_health._score_no_verdict(run, {}) is None


def test_role_health_no_changes_helper_preserves_no_changes_classification():
    run = {"step": "dev"}

    assert role_health._no_changes_result(run, "failed", "no_work", "", {"no_changes": True})
    assert not role_health._no_changes_result(run, "failed", "", "worker error", {})


def test_role_health_tool_missing_helper_excludes_unverified_evidence():
    assert role_health._score_tool_missing({"details": {}}).failure is None


def test_role_health_development_helper_preserves_decision_scoring():
    assert role_health._score_development({"pr_url": "https://example.test/pr/1"},
                                          {"pr_url_source": "current"}).failure == 0.0
    sent_back = role_health._score_development({"decision": "rework"}, {})
    assert sent_back.label == "sent_back" and sent_back.failure == 0.5


def test_role_health_other_helper_preserves_failure_and_stop_scoring():
    assert role_health._score_other("failed", "").failure == 1.0
    assert role_health._score_other("timeout", "").failure == role_health.STOPPED_WEIGHT
