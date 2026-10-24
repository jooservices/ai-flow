"""Merged means Done (JAIF-267): the flow merges approved tickets and Jira follows GitHub whoever merges."""
import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from conftest import add_ticket
from aiflow.v4.control import loops, merge_flow
from aiflow.v4.control.db import J

PR_URL = "https://github.com/jooservices/test-aiflow-wp/pull/7"
HEAD = "headsha7"


def add_pr_ticket(ctx, key, status, pr_url=PR_URL, labels=()):
    add_ticket(ctx.db, key, status=status, labels=labels)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,pr_url,"
               "details) VALUES (%s,%s,'TESTWP','dev','codex-01','codex','node1',1,'finished','done',%s,%s)",
               (str(uuid.uuid4()), key, pr_url, J({})))
    ctx.jira.status_of[key] = status


def set_pr(ctx, number=7, **fields):
    pr = {"number": number, "state": "open", "merged": False, "mergeable_state": "clean", "head": {"sha": HEAD}}
    pr.update(fields)
    ctx.github.pr_data = {number: pr}
    return pr


def moves(ctx, key):
    return ctx.db.all("SELECT from_status, to_status, cause, detail FROM ticket_moves WHERE ticket=%s ORDER BY id", (key,))


def ticket(ctx, key):
    return ctx.db.one("SELECT * FROM tickets WHERE key=%s", (key,))


# --- AC1: an approved ticket is merged by the flow ---------------------------------------------------------------
def test_ready_to_merge_ticket_is_updated_awaited_merged_and_done(ctx):
    add_pr_ticket(ctx, "TESTWP-701", "Ready to merge")
    set_pr(ctx, mergeable_state="behind")

    assert merge_flow.merge_ready(ctx) == []  # behind: the branch is updated first
    assert ctx.github.branch_updates == [("test-aiflow-wp", 7)]

    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "pending", "url": ""}]}
    assert merge_flow.merge_ready(ctx) == []  # the checks are awaited
    assert not hasattr(ctx.github, "merged")
    assert "checks" in ticket(ctx, "TESTWP-701")["merge_note"]

    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "success", "url": ""}]}
    assert merge_flow.merge_ready(ctx) == ["TESTWP-701"]

    assert ctx.github.merged == [("test-aiflow-wp", 7, "TESTWP-701: Summary of TESTWP-701", "squash")]
    assert ctx.jira.status_of["TESTWP-701"] == "Done"
    row = ticket(ctx, "TESTWP-701")
    assert row["status"] == "Done" and row["pr_state"] == "merged" and row["done_at"] is not None
    assert row["merged_at"] is not None and row["merge_sha"] == "abc123" and row["approved_at"] is not None
    assert merge_flow.facts(row)["approval_to_merge_s"] >= 0  # AC5
    assert moves(ctx, "TESTWP-701")[-1] == {"from_status": "Ready to merge", "to_status": "Done", "cause": "flow merge",
                                            "detail": "PR #7 merged as abc123"}  # AC6
    assert any(k == "TESTWP-701" and "merged" in body for k, body in ctx.jira.comments)


def test_failing_check_sends_the_ticket_back_with_the_reason(ctx):
    add_pr_ticket(ctx, "TESTWP-702", "Ready to merge")
    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Unit", "state": "failure", "url": "https://ci/1"}]}

    assert merge_flow.merge_ready(ctx) == []

    assert ctx.jira.status_of["TESTWP-702"] == "To Do"
    assert "Checks failed: Unit" in ticket(ctx, "TESTWP-702")["merge_note"]


# --- AC2: a conflict goes back to development --------------------------------------------------------------------
def test_conflicting_branch_goes_to_development_with_the_conflict_recorded(ctx):
    add_pr_ticket(ctx, "TESTWP-703", "Ready to merge")
    set_pr(ctx, mergeable_state="dirty")

    assert merge_flow.merge_ready(ctx) == []

    assert ctx.jira.status_of["TESTWP-703"] == "To Do"
    row = ticket(ctx, "TESTWP-703")
    assert row["status"] == "To Do" and "conflicts" in row["merge_note"] and "rework" in row["labels"]
    assert any(k == "TESTWP-703" and "conflicts" in body for k, body in ctx.jira.comments)
    assert any(number == 7 and "conflicts" in body for number, body in ctx.github.comments)
    run = ctx.db.one("SELECT outcome, details FROM runs WHERE ticket_key='TESTWP-703' AND step='qa'")
    assert run["outcome"] == "rework" and "conflicts" in run["details"]["findings"]  # the next dev run receives it
    assert moves(ctx, "TESTWP-703")[-1]["cause"] == "flow merge"
    assert not hasattr(ctx.github, "merged")


def test_conflict_found_while_updating_the_branch_goes_to_development(ctx):
    add_pr_ticket(ctx, "TESTWP-704", "Ready to merge")
    set_pr(ctx, mergeable_state="behind")
    ctx.github.update_branch_result = {"ok": False, "conflict": True,
                                       "message": "422: merge conflict between base and head"}

    merge_flow.merge_ready(ctx)

    assert ctx.jira.status_of["TESTWP-704"] == "To Do"


# --- the owner's approval in human review ------------------------------------------------------------------------
def test_approved_in_human_review_but_not_mergeable_yet_is_merged_by_the_flow(client, ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-705", "Human review", pr_url="https://github.com/jooservices/test-aiflow-wp/pull/42",
                  labels=("require-human",))
    real_merge = ctx.github.merge_pr
    monkeypatch.setattr(ctx.github, "merge_pr", lambda *a, **k: {"merged": False, "message": "required checks are pending"})

    response = client.post("/v1/reviews/TESTWP-705/approve", headers={"Authorization": "Bearer review-secret"})

    assert response.status_code == 200 and response.json()["merged"] is False and response.json()["queued"] is True
    row = ticket(ctx, "TESTWP-705")
    assert row["status"] == "Ready to merge" and row["approved_at"] is not None
    assert moves(ctx, "TESTWP-705")[-1]["cause"] == "person"

    monkeypatch.setattr(ctx.github, "merge_pr", real_merge)
    set_pr(ctx, 42)
    assert merge_flow.merge_ready(ctx) == ["TESTWP-705"]
    assert ctx.jira.status_of["TESTWP-705"] == "Done"
    assert moves(ctx, "TESTWP-705")[-1]["cause"] == "flow merge"


def test_approval_that_merges_at_once_keeps_the_merge_facts(client, ctx):
    add_pr_ticket(ctx, "TESTWP-706", "Human review", pr_url="https://github.com/jooservices/test-aiflow-wp/pull/42",
                  labels=("require-human",))

    response = client.post("/v1/reviews/TESTWP-706/approve", headers={"Authorization": "Bearer review-secret"})

    assert response.json()["merged"] is True
    row = ticket(ctx, "TESTWP-706")
    assert row["status"] == "Done" and row["merged_at"] is not None and row["approved_at"] is not None
    assert row["merge_sha"] == "abc123"


# --- AC3 / AC4: Jira follows GitHub whoever merges ---------------------------------------------------------------
def test_merge_by_a_person_is_detected_and_the_ticket_is_done_with_the_merge_time(ctx):
    add_pr_ticket(ctx, "TESTWP-710", "In Verify")
    set_pr(ctx)
    merge_flow.reconcile(ctx)
    assert ticket(ctx, "TESTWP-710")["status"] == "In Verify" and ticket(ctx, "TESTWP-710")["pr_state"] == "open"

    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="c0ffee1234567",
           merged_by={"login": "somebody"})
    assert merge_flow.reconcile(ctx)["moved"] == 1

    row = ticket(ctx, "TESTWP-710")
    assert row["status"] == "Done" and ctx.jira.status_of["TESTWP-710"] == "Done"
    assert row["merged_at"].isoformat().startswith("2026-10-23T10:00:00") and row["done_at"] == row["merged_at"]
    assert row["merge_sha"] == "c0ffee1234567" and row["merged_by"] == "somebody"
    assert moves(ctx, "TESTWP-710")[-1] == {"from_status": "In Verify", "to_status": "Done", "cause": "reconcile",
                                            "detail": "PR #7 merged on GitHub by somebody"}
    assert any(k == "TESTWP-710" and "merged on GitHub" in body for k, body in ctx.jira.comments)


def test_the_reconcile_loop_runs_every_minute_and_before_the_next_minute_ends(ctx, monkeypatch):
    assert merge_flow.RECONCILE_EVERY_S <= 60 and merge_flow.MERGE_EVERY_S <= 60
    add_pr_ticket(ctx, "TESTWP-711", "In Verify")
    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="abc")
    ctx.db.run("UPDATE tickets SET pr_state='open' WHERE key='TESTWP-711'")
    done, stop = threading.Event(), threading.Event()
    real = merge_flow.reconcile

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        done.set()
        return result

    monkeypatch.setattr(loops.merge_flow, "reconcile", spy)
    monkeypatch.setattr(loops, "jira_sync", lambda *a, **k: 0)
    monkeypatch.setattr(loops, "watchdog", lambda *a, **k: None)
    thread = threading.Thread(target=loops.run_loops, args=(ctx, stop), daemon=True)
    thread.start()
    try:
        assert done.wait(20), "the reconcile loop did not run on its first pass"
    finally:
        stop.set()
        thread.join(10)
    assert ticket(ctx, "TESTWP-711")["status"] == "Done"


def test_pull_request_closed_without_merge_sends_the_ticket_back_with_the_reason(ctx):
    add_pr_ticket(ctx, "TESTWP-720", "In Verify")
    set_pr(ctx)
    merge_flow.reconcile(ctx)

    set_pr(ctx, state="closed")
    merge_flow.reconcile(ctx)

    row = ticket(ctx, "TESTWP-720")
    assert row["status"] == "To Do" and ctx.jira.status_of["TESTWP-720"] == "To Do"
    assert "closed without being merged" in row["merge_note"]
    assert moves(ctx, "TESTWP-720")[-1]["cause"] == "reconcile"
    assert any(k == "TESTWP-720" and "closed without being merged" in body for k, body in ctx.jira.comments)
    run = ctx.db.one("SELECT outcome, details FROM runs WHERE ticket_key='TESTWP-720' AND step='qa'")
    assert run["outcome"] == "rework" and "closed" in run["details"]["findings"]
    merge_flow.reconcile(ctx)  # nothing changed: the ticket is not moved again
    assert len(moves(ctx, "TESTWP-720")) == 1


def test_reopened_pull_request_returns_the_ticket_to_in_verify(ctx):
    add_pr_ticket(ctx, "TESTWP-721", "In Verify")
    set_pr(ctx)
    merge_flow.reconcile(ctx)
    set_pr(ctx, state="closed")
    merge_flow.reconcile(ctx)
    assert ticket(ctx, "TESTWP-721")["status"] == "To Do"

    set_pr(ctx)  # reopened
    merge_flow.reconcile(ctx)

    assert ticket(ctx, "TESTWP-721")["status"] == "In Verify" and ctx.jira.status_of["TESTWP-721"] == "In Verify"
    assert [m["to_status"] for m in moves(ctx, "TESTWP-721")] == ["To Do", "In Verify"]


def test_a_closed_pull_request_of_a_done_ticket_is_left_alone_on_the_first_look(ctx):
    add_pr_ticket(ctx, "TESTWP-722", "Done", pr_url="https://github.com/jooservices/test-aiflow-wp/pull/8")
    set_pr(ctx, 8, state="closed")  # a rejected ticket from before this release

    merge_flow.reconcile(ctx)

    assert ticket(ctx, "TESTWP-722")["status"] == "Done" and not moves(ctx, "TESTWP-722")


def test_a_merged_pull_request_puts_the_ticket_in_done_from_any_state(ctx):
    for key, status, number in (("TESTWP-723", "To Do", 9), ("TESTWP-725", "Backlog", 10), ("TESTWP-726", "Intake", 11)):
        add_pr_ticket(ctx, key, status, pr_url=f"https://github.com/jooservices/test-aiflow-wp/pull/{number}")
        ctx.github.pr_data = {**getattr(ctx.github, "pr_data", {}), number: {
            "number": number, "state": "closed", "merged": True, "merged_at": "2026-10-23T08:00:00Z",
            "merge_commit_sha": f"sha{number}"}}

    assert merge_flow.reconcile(ctx)["moved"] == 3

    for key in ("TESTWP-723", "TESTWP-725", "TESTWP-726"):
        assert ticket(ctx, key)["status"] == "Done" and ctx.jira.status_of[key] == "Done"
        assert moves(ctx, key)[-1]["cause"] == "reconcile"


def test_a_ticket_moved_out_of_done_after_the_merge_is_put_back(ctx):
    add_pr_ticket(ctx, "TESTWP-727", "In Verify")
    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="abc")
    merge_flow.reconcile(ctx)
    assert ticket(ctx, "TESTWP-727")["pr_state"] == "merged"
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='TESTWP-727'")  # synced from Jira after a stray move
    ctx.jira.status_of["TESTWP-727"] = "In Verify"

    merge_flow.reconcile(ctx)

    assert ticket(ctx, "TESTWP-727")["status"] == "Done" and ctx.jira.status_of["TESTWP-727"] == "Done"


def test_a_disagreement_is_cleared_only_after_jira_reaches_the_target(ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-728", "In Verify")
    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="abc")
    monkeypatch.setattr(ctx.jira, "transition_to", lambda key, target: "In Verify")  # Jira refuses to move on

    merge_flow.reconcile(ctx)

    assert ticket(ctx, "TESTWP-728")["status"] == "In Verify"
    assert ctx.db.one("SELECT expected FROM ticket_disagreements WHERE ticket='TESTWP-728'")["expected"] == "Done"
    monkeypatch.undo()
    merge_flow.reconcile(ctx)
    assert ticket(ctx, "TESTWP-728")["status"] == "Done"
    assert not ctx.db.one("SELECT 1 FROM ticket_disagreements WHERE ticket='TESTWP-728'")


def test_a_rejected_ticket_stays_done_when_its_pull_request_is_closed(client, ctx):
    add_pr_ticket(ctx, "TESTWP-724", "Human review", pr_url="https://github.com/jooservices/test-aiflow-wp/pull/42",
                  labels=("require-human",))
    response = client.post("/v1/reviews/TESTWP-724/reject", json={"reason": "wrong approach"},
                           headers={"Authorization": "Bearer review-secret"})
    assert response.status_code == 200
    set_pr(ctx, 42, state="closed")

    merge_flow.reconcile(ctx)

    assert ticket(ctx, "TESTWP-724")["status"] == "Done"


# --- AC7: disagreements are listed after five minutes and fixed ---------------------------------------------------
def test_a_disagreement_older_than_five_minutes_is_listed_and_then_fixed(client, ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-730", "In Verify")
    set_pr(ctx)
    merge_flow.reconcile(ctx)
    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="abc")
    monkeypatch.setattr(ctx.jira, "transition_to", lambda *a: (_ for _ in ()).throw(RuntimeError("Jira is down")))

    merge_flow.reconcile(ctx)  # the fix fails: Jira still says In Verify
    assert ticket(ctx, "TESTWP-730")["status"] == "In Verify"
    assert client.get("/v1/dashboard/disagreements").json()["tickets"] == []  # not five minutes yet

    ctx.db.run("UPDATE ticket_disagreements SET since = now() - interval '6 minutes'")
    listed = client.get("/v1/dashboard/disagreements").json()["tickets"]
    assert [r["key"] for r in listed] == ["TESTWP-730"]
    assert listed[0]["jira_status"] == "In Verify" and listed[0]["github_state"] == "merged" and listed[0]["expected"] == "Done"
    assert "Jira is down" in listed[0]["detail"]

    monkeypatch.undo()  # Jira works again: the flow fixes the ticket itself
    merge_flow.reconcile(ctx)
    assert ticket(ctx, "TESTWP-730")["status"] == "Done"
    assert client.get("/v1/dashboard/disagreements").json()["tickets"] == []


# --- AC6: every move records its cause ---------------------------------------------------------------------------
def test_a_move_made_in_jira_is_recorded_as_a_persons(ctx):
    from conftest import FakeJira  # noqa: F401
    from aiflow.v4.control.jira import Jira
    add_ticket(ctx.db, "TESTWP-740", status="In Verify", labels=())
    ctx.jira.search = lambda _jql: [{"key": "TESTWP-740", "fields": {
        "summary": "S", "status": {"name": "To Do"}, "issuetype": {"name": "Task"}, "labels": []}}]
    ctx.jira.normalize = Jira.normalize

    loops.jira_sync(ctx, full=True)

    assert moves(ctx, "TESTWP-740") == [{"from_status": "In Verify", "to_status": "To Do", "cause": "person",
                                         "detail": "moved in Jira"}]


def test_every_cause_is_one_of_the_named_ones():
    assert set(merge_flow.CAUSES) == {"flow merge", "person", "reconcile", "flow"}


def test_the_watchdog_records_its_requeue_of_an_orphan_as_a_flow_move(ctx):
    add_ticket(ctx.db, "TESTWP-741", status="In Progress")
    ctx.db.run("UPDATE tickets SET synced_at=now() WHERE key='TESTWP-741'")
    ctx.jira.status_of["TESTWP-741"] = "In Progress"

    loops.watchdog(ctx)

    assert ticket(ctx, "TESTWP-741")["status"] == "To Do"
    assert moves(ctx, "TESTWP-741")[-1]["cause"] == "flow"
    assert moves(ctx, "TESTWP-741")[-1]["from_status"] == "In Progress" and moves(ctx, "TESTWP-741")[-1]["to_status"] == "To Do"
    assert "orphan" not in moves(ctx, "TESTWP-741")[-1]["detail"] and "watchdog" in moves(ctx, "TESTWP-741")[-1]["detail"]


def test_the_watchdog_records_its_requeue_of_a_lost_lease_as_a_flow_move(ctx):
    add_ticket(ctx.db, "TESTWP-742", status="In Progress")
    ctx.jira.status_of["TESTWP-742"] = "In Progress"
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status) VALUES"
               " (%s,'TESTWP-742','TESTWP','dev','codex-01','codex','node1',3,'running')", (str(uuid.uuid4()),))

    loops.watchdog(ctx)  # no lease is held for the running run: it is lost

    assert ticket(ctx, "TESTWP-742")["status"] == "To Do"
    last = moves(ctx, "TESTWP-742")[-1]
    assert last["cause"] == "flow" and "lost its lease" in last["detail"]
    # the later Jira sync sees no status change, so it adds no second, wrong cause
    assert [m["cause"] for m in moves(ctx, "TESTWP-742")] == ["flow"]


# --- review findings: pinned merge, leases, merge time, the one-minute bound --------------------------------------
def test_the_merge_is_pinned_to_the_commit_whose_checks_were_read(ctx):
    add_pr_ticket(ctx, "TESTWP-750", "Ready to merge")
    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "success", "url": ""}]}

    assert merge_flow.merge_ready(ctx) == ["TESTWP-750"]

    assert ctx.github.merge_pins == [HEAD]


def test_a_branch_that_moved_after_its_checks_were_read_is_not_merged_and_is_checked_again(ctx):
    add_pr_ticket(ctx, "TESTWP-751", "Ready to merge")
    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "success", "url": ""}],
                            "newhead": [{"name": "Test", "state": "pending", "url": ""}]}
    real_checks = ctx.github.checks

    def checks_then_push(repo, sha):
        result = real_checks(repo, sha)
        ctx.github.pr_data[7]["head"] = {"sha": "newhead"}  # someone pushes while the flow is about to merge
        return result

    ctx.github.checks = checks_then_push

    assert merge_flow.merge_ready(ctx) == []
    assert not hasattr(ctx.github, "merged") and ticket(ctx, "TESTWP-751")["status"] == "Ready to merge"
    assert "changed after its checks" in ticket(ctx, "TESTWP-751")["merge_note"]

    assert merge_flow.merge_ready(ctx) == []  # the new commit has pending checks
    assert "checks are still running" in ticket(ctx, "TESTWP-751")["merge_note"]
    ctx.github.check_data["newhead"] = [{"name": "Test", "state": "success", "url": ""}]
    assert merge_flow.merge_ready(ctx) == ["TESTWP-751"] and ctx.github.merge_pins[-1] == "newhead"


def test_reconcile_does_not_write_jira_while_a_run_holds_the_ticket_and_does_when_it_ends(ctx):
    add_pr_ticket(ctx, "TESTWP-752", "In Verify")
    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="abc")
    claim = ctx.leases.claim("TESTWP-752", "worker-run", 60_000)
    assert claim.ok

    merge_flow.reconcile(ctx)

    assert ctx.jira.status_of["TESTWP-752"] == "In Verify" and not ctx.jira.transitions
    assert "a run holds the ticket" in ctx.db.one("SELECT detail FROM ticket_disagreements WHERE ticket='TESTWP-752'")["detail"]
    assert ctx.leases.valid("TESTWP-752", "worker-run", claim.fence)  # the worker's fence is untouched
    ctx.leases.release("TESTWP-752", "worker-run", claim.fence)

    merge_flow.reconcile(ctx)

    assert ticket(ctx, "TESTWP-752")["status"] == "Done"
    assert ctx.leases.holder("TESTWP-752") is None  # the control plane gave the lease back


def test_the_lease_is_renewed_before_every_outside_call_of_the_merge_step(ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-753", "Ready to merge")
    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "success", "url": ""}]}
    order = []
    real_renew = ctx.leases.renew
    monkeypatch.setattr(ctx.leases, "renew", lambda *a, **k: (order.append("renew"), real_renew(*a, **k))[1])
    real_merge, real_transition = ctx.github.merge_pr, ctx.jira.transition_to
    monkeypatch.setattr(ctx.github, "merge_pr", lambda *a, **k: (order.append("merge"), real_merge(*a, **k))[1])
    monkeypatch.setattr(ctx.jira, "transition_to", lambda *a: (order.append("jira"), real_transition(*a))[1])

    assert merge_flow.merge_ready(ctx) == ["TESTWP-753"]

    assert order[order.index("merge") - 1] == "renew" and order[order.index("jira") - 1] == "renew"


def test_a_lost_lease_stops_the_merge_step_before_it_writes_jira(ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-754", "Ready to merge")
    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "success", "url": ""}]}
    monkeypatch.setattr(ctx.leases, "renew", lambda *a, **k: False)

    assert merge_flow.merge_ready(ctx) == []

    assert not ctx.jira.transitions and "lease" in ticket(ctx, "TESTWP-754")["merge_note"]


def test_the_stored_merge_time_is_githubs_even_when_the_jira_move_is_late(ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-755", "Ready to merge")
    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "success", "url": ""}]}
    ctx.db.run("UPDATE tickets SET approved_at=%s WHERE key='TESTWP-755'", (datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc),))
    real_merge, real_transition = ctx.github.merge_pr, ctx.jira.transition_to

    def merge(*a, **k):
        result = real_merge(*a, **k)
        ctx.github.pr_data[7].update({"merged": True, "state": "closed", "merged_at": "2026-10-23T09:00:30Z"})
        return result

    def late_transition(*a):
        time.sleep(0.4)  # Jira answers late
        return real_transition(*a)

    monkeypatch.setattr(ctx.github, "merge_pr", merge)
    monkeypatch.setattr(ctx.jira, "transition_to", late_transition)

    assert merge_flow.merge_ready(ctx) == ["TESTWP-755"]
    merge_flow.reconcile(ctx)  # a later reconcile keeps GitHub's time

    row = ticket(ctx, "TESTWP-755")
    assert row["merged_at"] == datetime(2026, 10, 23, 9, 0, 30, tzinfo=timezone.utc) and row["done_at"] == row["merged_at"]
    assert merge_flow.facts(row)["approval_to_merge_s"] == 30


def test_without_a_github_merge_time_the_clock_right_after_the_merge_is_kept_not_the_jira_move(ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-756", "Ready to merge")
    set_pr(ctx)
    ctx.github.check_data = {HEAD: [{"name": "Test", "state": "success", "url": ""}]}
    real_transition, moved_at = ctx.jira.transition_to, []

    def late_transition(*a):
        time.sleep(0.4)
        moved_at.append(datetime.now(timezone.utc))
        return real_transition(*a)

    monkeypatch.setattr(ctx.jira, "transition_to", late_transition)
    assert merge_flow.merge_ready(ctx) == ["TESTWP-756"]

    assert (moved_at[0] - ticket(ctx, "TESTWP-756")["merged_at"]).total_seconds() >= 0.35


def test_the_bounds_of_the_reconcile_loop_stay_inside_one_minute():
    assert merge_flow.RECONCILE_EVERY_S + merge_flow.RECONCILE_BUDGET_S < merge_flow.RECONCILE_BOUND_S <= 60


def test_a_backlog_does_not_hold_back_a_merge_and_every_pull_request_is_reached_within_the_bound(ctx):
    """Fake clock: every GitHub read takes 7 s, a pass may start work for 20 s, a pass starts every 20 s."""
    now = [0.0]
    numbers = list(range(100, 112))
    for n in numbers:
        add_pr_ticket(ctx, f"TESTWP-{n}", "In Verify", pr_url=f"https://github.com/jooservices/test-aiflow-wp/pull/{n}")
    ctx.github.pr_data = {n: {"number": n, "state": "open", "merged": False, "mergeable_state": "clean",
                              "head": {"sha": f"h{n}"}} for n in numbers}
    read_at: dict[int, float] = {}
    real_pr = ctx.github.pr

    def slow_pr(repo, number):
        read_at[number] = now[0]
        now[0] += 7
        return real_pr(repo, number)

    ctx.github.pr = slow_pr
    merged_at_pass = None
    for passes in range(1, 8):
        now[0] = (passes - 1) * merge_flow.RECONCILE_EVERY_S
        if passes == 3:  # a person merges the last pull request of the queue
            ctx.github.pr_data[111].update({"state": "closed", "merged": True, "merged_at": "2026-10-23T10:00:00Z",
                                            "merge_commit_sha": "x"})
            merged_at_pass = (now[0], passes)
        out = merge_flow.reconcile(ctx, workers=1, clock=lambda: now[0], budget_s=merge_flow.RECONCILE_BUDGET_S)
        assert out["checked"] + out["skipped"] == len(numbers) and out["checked"] >= 3
        if ticket(ctx, "TESTWP-111")["status"] == "Done":
            break
    # merged just before pass 3 starts; Done at the latest by the end of that pass or the next ones, never later
    assert ticket(ctx, "TESTWP-111")["status"] == "Done"
    assert now[0] - merged_at_pass[0] <= merge_flow.RECONCILE_BOUND_S
    assert set(read_at) == set(numbers)  # no pull request starved


def test_the_reconcile_loop_is_not_delayed_by_a_slow_jira_sync(ctx, monkeypatch):
    add_pr_ticket(ctx, "TESTWP-760", "In Verify")
    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="abc")
    ctx.db.run("UPDATE tickets SET pr_state='open' WHERE key='TESTWP-760'")
    release, stop = threading.Event(), threading.Event()
    monkeypatch.setattr(loops, "jira_sync", lambda *a, **k: release.wait(30) and 0)  # the main loop is stuck here
    monkeypatch.setattr(loops, "watchdog", lambda *a, **k: None)
    thread = threading.Thread(target=loops.run_loops, args=(ctx, stop), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and ticket(ctx, "TESTWP-760")["status"] != "Done":
            time.sleep(0.1)
        assert ticket(ctx, "TESTWP-760")["status"] == "Done"
    finally:
        release.set()
        stop.set()
        thread.join(10)


# --- webhook (optional) ------------------------------------------------------------------------------------------
def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_is_off_without_a_secret(client):
    assert client.post("/v1/github/webhook", content=b"{}").status_code == 404


def test_webhook_reconciles_the_pull_request_at_once(client, ctx):
    ctx.settings.github_webhook_secret = "hook-secret"
    add_pr_ticket(ctx, "TESTWP-750", "In Verify")
    set_pr(ctx)
    merge_flow.reconcile(ctx)
    set_pr(ctx, state="closed", merged=True, merged_at="2026-10-23T10:00:00Z", merge_commit_sha="abc")
    body = json.dumps({"action": "closed", "number": 7, "pull_request": {"number": 7},
                       "repository": {"full_name": "jooservices/test-aiflow-wp"}}).encode()
    headers = {"X-GitHub-Event": "pull_request"}

    assert client.post("/v1/github/webhook", content=body, headers={**headers, "X-Hub-Signature-256": "sha256=bad"}
                       ).status_code == 401
    assert ticket(ctx, "TESTWP-750")["status"] == "In Verify"

    ok = client.post("/v1/github/webhook", content=body,
                     headers={**headers, "X-Hub-Signature-256": sign("hook-secret", body)})

    assert ok.status_code == 200 and ok.json()["reconciled"] == 1
    assert ticket(ctx, "TESTWP-750")["status"] == "Done"


# --- AC5 / UI paths ----------------------------------------------------------------------------------------------
def test_ticket_details_show_the_pull_request_merge_commit_and_time_to_merge(client, ctx):
    add_pr_ticket(ctx, "TESTWP-760", "Ready to merge")
    set_pr(ctx)
    merge_flow.merge_ready(ctx)
    from aiflow.v4.control.ui import mount_ui
    mount_ui(client.app, ctx)

    data = client.get("/v1/tickets/TESTWP-760").json()

    assert data["merge"]["pr_url"] == PR_URL and data["merge"]["merge_sha"] == "abc123"
    assert data["merge"]["merged_at"] and data["merge"]["approval_to_merge_s"] is not None
    assert data["moves"][0]["cause"] == "flow merge"


def test_the_console_has_the_dashboard_list_and_the_ticket_merge_details():
    page = (Path(merge_flow.__file__).parent / "ui" / "console.html").read_text(encoding="utf-8")
    assert "/v1/dashboard/disagreements" in page and "Jira and GitHub disagree" in page
    assert "Approval to merge" in page and "Merge commit" in page and "Moves and their cause" in page
