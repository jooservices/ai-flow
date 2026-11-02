"""Characterize _merge_held's branch results and side-effect order before extraction."""
from __future__ import annotations

from aiflow.v4.control import merge_flow


def _open_pr():
    return {"merged": False, "state": "open", "mergeable_state": "clean", "head": {"sha": "head-1"}}


CASES = [
    {"name": "initial_merged", "initial_pr": {"merged": True}},
    {"name": "initial_closed", "initial_pr": {"state": "closed"}},
    {"name": "preflight_updated", "preflight": {"updated": True, "message": "branch updated"}},
    {"name": "preflight_conflict", "preflight": {"conflict": True}},
    {"name": "preflight_queued", "preflight": {"queued": True, "message": "ahead of TESTWP-1"}},
    {"name": "preflight_update_failed", "preflight": {"update_failed": True, "message": "GitHub unavailable"}},
    {"name": "preflight_updated_default_message", "preflight": {"updated": True}},
    {"name": "preflight_queued_default_message", "preflight": {"queued": True}},
    {"name": "preflight_update_failed_default_message", "preflight": {"update_failed": True}},
    {"name": "preflight_multiple_flags", "preflight": {"updated": True, "conflict": True, "queued": True,
                                                           "update_failed": True, "message": "priority"}},
    {"name": "after_preflight_merged", "second_pr": {"merged": True}},
    {"name": "after_preflight_closed", "second_pr": {"state": "closed"}},
    {"name": "dirty_after_preflight", "second_pr": {**_open_pr(), "mergeable_state": "dirty"}},
    {"name": "one_failed_check", "checks": [{"name": "Unit", "url": "https://ci/jobs/1", "state": "failure"}]},
    {"name": "failed_checks_limit", "checks": [
        {"name": f"Check-{n}", "url": f"https://ci/jobs/{n}", "state": "failure"} for n in range(1, 10)
    ]},
    {"name": "pending_check", "checks": [{"name": "Unit", "url": "", "state": "pending"}]},
    {"name": "missing_head", "second_pr": {"mergeable_state": "clean", "head": {}}},
    {"name": "final_queued", "final_result": {"queued": True, "message": "ahead of TESTWP-2"}, "invoke_merge": False},
    {"name": "final_updated", "final_result": {"updated": True, "message": "branch updated after checks"}, "invoke_merge": False},
    {"name": "final_conflict", "final_result": {"conflict": True}, "invoke_merge": False},
    {"name": "final_update_failed", "final_result": {"update_failed": True, "message": "update rejected"}, "invoke_merge": False},
    {"name": "final_queue_default_message", "final_result": {"queued": True}, "invoke_merge": False},
    {"name": "final_update_failed_default_message", "final_result": {"update_failed": True}, "invoke_merge": False},
    {"name": "final_multiple_flags", "final_result": {"queued": True, "updated": True, "conflict": True,
                                                          "update_failed": True, "message": "priority"}, "invoke_merge": False},
    {"name": "refused_dirty", "merge_result": {"merged": False, "mergeable_state": "dirty", "message": "conflict"}},
    {"name": "refused_409_prefix", "merge_result": {"merged": False, "message": "409: head changed"}},
    {"name": "refused_409_with_leading_space", "merge_result": {"merged": False, "message": " 409: head changed"}},
    {"name": "refused_generic", "merge_result": {"merged": False, "message": "required checks pending"}},
    {"name": "refused_empty_message", "merge_result": {"merged": False}},
    {"name": "merged_with_sha", "merge_result": {"merged": True, "sha": "merge-1234567890"},
     "merged_pr": {"merged_at": "2026-10-20T11:12:13Z"}, "seconds": 37},
    {"name": "merged_without_sha_or_time", "merge_result": {"merged": True}, "merged_pr": {"merged_at": "bad-time"}},
    {"name": "merged_time_read_error", "merge_result": {"merged": True, "sha": "merge-2"},
     "pr_error_at": 3, "pr_error": "GitHub read failed"},
    {"name": "merge_api_message_only_sha", "merge_result": {"merged": True, "message": "done"},
     "merge_method": "merge"},
    {"name": "keep_lost_before_first_read", "keep_error_at": 1},
    {"name": "keep_lost_while_reading_merge_time", "merge_result": {"merged": True, "sha": "merge-3"},
     "keep_error_at": 5},
]

# Literal return values and call traces printed by the fake harness against the unchanged function.
EXPECTED = {
    "initial_merged": (True, "keep\npr:test-aiflow-wp#7\nqueue:finish:True\nreconcile"),
    "initial_closed": (True, "keep\npr:test-aiflow-wp#7\nqueue:finish:False\nreconcile"),
    "preflight_updated": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\naudit:github.update-branch:True:branch updated\nevent:github.merge_queue:updated:branch updated\nnote:waiting: the branch was updated, the checks run again"),
    "preflight_conflict": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nsend:The branch conflicts with the base branch."),
    "preflight_queued": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nnote:waiting: ahead of TESTWP-1"),
    "preflight_update_failed": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nnote:waiting: GitHub unavailable"),
    "preflight_updated_default_message": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\naudit:github.update-branch:True:branch brought up to date with the base\nevent:github.merge_queue:updated:branch brought up to date with the base\nnote:waiting: the branch was updated, the checks run again"),
    "preflight_queued_default_message": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nnote:waiting: the repository merge queue is busy"),
    "preflight_update_failed_default_message": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nnote:waiting: the branch update failed"),
    "preflight_multiple_flags": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\naudit:github.update-branch:True:priority\nevent:github.merge_queue:updated:priority\nnote:waiting: the branch was updated, the checks run again"),
    "after_preflight_merged": (True, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nqueue:finish:True\nreconcile"),
    "after_preflight_closed": (True, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nqueue:finish:False\nreconcile"),
    "dirty_after_preflight": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nqueue:leave\nsend:The branch conflicts with the base branch."),
    "one_failed_check": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nqueue:leave\nsend:Checks failed: Unit (https://ci/jobs/1)"),
    "failed_checks_limit": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nqueue:leave\nsend:Checks failed: Check-1 (https://ci/jobs/1), Check-2 (https://ci/jobs/2), Check-3 (https://ci/jobs/3), Check-4 (https://ci/jobs/4), Check-5 (https://ci/jobs/5), Check-6 (https://ci/jobs/6), Check-7 (https://ci/jobs/7), Check-8 (https://ci/jobs/8)"),
    "pending_check": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nnote:waiting: the checks are still running"),
    "missing_head": (True, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:\naudit:github.merge:True:merge-1234567890\nevent:github.merge:merged:\nkeep\npr:test-aiflow-wp#7\nrecord_merge:merge-1234567890:settled=False\nmove:Done:flow merge:PR #7 merged as merge-1234\npr_state:merged\nticket_row\nkeep\ncomment:AI Flow · merge · merged\nWhat: PR #7 merged as merge-1234 (https://github.com/jooservices/test-aiflow-wp/pull/7).\nWhy: The ticket was approved and the pull request could merge.\nNext: Ticket Done.\nnote_run:merged:PR #7 merged by the flow"),
    "final_queued": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nnote:waiting: ahead of TESTWP-2"),
    "final_updated": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\naudit:github.update-branch:True:branch updated after checks\nevent:github.merge_queue:updated:branch updated after checks\nnote:waiting: the branch was updated, the checks run again"),
    "final_conflict": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nsend:The branch conflicts with the base branch."),
    "final_update_failed": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nnote:waiting: update rejected"),
    "final_queue_default_message": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nnote:waiting: the repository merge queue is busy"),
    "final_update_failed_default_message": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nnote:waiting: the branch update failed"),
    "final_multiple_flags": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nnote:waiting: priority"),
    "refused_dirty": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:False:conflict\nevent:github.merge:refused:conflict\nqueue:leave\nsend:The branch conflicts with the base branch."),
    "refused_409_prefix": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:False:409: head changed\nevent:github.merge:refused:409: head changed\nnote:waiting: the branch changed after its checks were read; it is checked again"),
    "refused_409_with_leading_space": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:False: 409: head changed\nevent:github.merge:refused: 409: head changed\nnote:waiting: GitHub refused the merge ( 409: head changed)"),
    "refused_generic": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:False:required checks pending\nevent:github.merge:refused:required checks pending\nnote:waiting: GitHub refused the merge (required checks pending)"),
    "refused_empty_message": (False, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:False:\nevent:github.merge:refused:\nnote:waiting: GitHub refused the merge ()"),
    "merged_with_sha": (True, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:True:merge-1234567890\nevent:github.merge:merged:\nkeep\npr:test-aiflow-wp#7\nrecord_merge:merge-1234567890:settled=False\nmove:Done:flow merge:PR #7 merged as merge-1234\npr_state:merged\nticket_row\nkeep\ncomment:AI Flow · merge · merged\nWhat: PR #7 merged as merge-1234 (https://github.com/jooservices/test-aiflow-wp/pull/7).\nWhy: The ticket was approved and the pull request could merge.\nNext: Ticket Done; 37 s between approval and merge.\nnote_run:merged:PR #7 merged by the flow"),
    "merged_without_sha_or_time": (True, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:True:\nevent:github.merge:merged:\nkeep\npr:test-aiflow-wp#7\nrecord_merge::settled=False\nmove:Done:flow merge:PR #7 merged as \npr_state:merged\nticket_row\nkeep\ncomment:AI Flow · merge · merged\nWhat: PR #7 merged as  (https://github.com/jooservices/test-aiflow-wp/pull/7).\nWhy: The ticket was approved and the pull request could merge.\nNext: Ticket Done.\nnote_run:merged:PR #7 merged by the flow"),
    "merged_time_read_error": (True, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:True:merge-2\nevent:github.merge:merged:\nkeep\npr:test-aiflow-wp#7\nwarning:could not read the merge time of PR #7: GitHub read failed\nrecord_merge:merge-2:settled=False\nmove:Done:flow merge:PR #7 merged as merge-2\npr_state:merged\nticket_row\nkeep\ncomment:AI Flow · merge · merged\nWhat: PR #7 merged as merge-2 (https://github.com/jooservices/test-aiflow-wp/pull/7).\nWhy: The ticket was approved and the pull request could merge.\nNext: Ticket Done.\nnote_run:merged:PR #7 merged by the flow"),
    "merge_api_message_only_sha": (True, "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:merge:head-1\naudit:github.merge:True:done\nevent:github.merge:merged:done\nkeep\npr:test-aiflow-wp#7\nrecord_merge::settled=False\nmove:Done:flow merge:PR #7 merged as \npr_state:merged\nticket_row\nkeep\ncomment:AI Flow · merge · merged\nWhat: PR #7 merged as  (https://github.com/jooservices/test-aiflow-wp/pull/7).\nWhy: The ticket was approved and the pull request could merge.\nNext: Ticket Done.\nnote_run:merged:PR #7 merged by the flow"),
    "keep_lost_before_first_read": ("raised:LeaseLost:lease lost", "keep"),
    "keep_lost_while_reading_merge_time": ("raised:LeaseLost:lease lost", "keep\npr:test-aiflow-wp#7\nqueue:preflight\nkeep\npr:test-aiflow-wp#7\nkeep\nchecks:head-1\nkeep\nqueue:merge\nmerge_pr:test-aiflow-wp#7:squash:head-1\naudit:github.merge:True:merge-3\nevent:github.merge:merged:\nkeep"),
}


class _Hold:
    def __init__(self, calls, error_at):
        self.calls, self.error_at, self.keeps = calls, error_at, 0

    def keep(self):
        self.keeps += 1
        self.calls.append("keep")
        if self.keeps == self.error_at:
            raise merge_flow.LeaseLost("lease lost")


class _Database:
    def __init__(self, calls):
        self.calls = calls

    def run(self, sql, params=()):
        if "merge_note" in sql:
            literal = sql.partition("merge_note='")[2].partition("'")[0]
            self.calls.append(f"note:{literal or params[0]}")
        elif "pr_state='merged'" in sql:
            self.calls.append("pr_state:merged")

    def one(self, sql, _params=()):
        if "SELECT * FROM tickets" in sql:
            self.calls.append("ticket_row")
            return {}


class _Gateway:
    def __init__(self, case, calls):
        self.case, self.calls, self.pr_count = case, calls, 0

    def pr(self, name, number):
        self.pr_count += 1
        self.calls.append(f"pr:{name}#{number}")
        if self.pr_count == self.case.get("pr_error_at"):
            raise RuntimeError(self.case["pr_error"])
        default = _open_pr()
        responses = [self.case.get("initial_pr", default), self.case.get("second_pr", default),
                     self.case.get("merged_pr", {"merged_at": "2026-10-20T11:12:13Z"})]
        return responses[min(self.pr_count - 1, len(responses) - 1)]

    def checks(self, name, head):
        self.calls.append(f"checks:{head}")
        return self.case.get("checks", [{"name": "Unit", "url": "", "state": "success"}])

    def merge_pr(self, name, number, title, method, sha=""):
        self.calls.append(f"merge_pr:{name}#{number}:{method}:{sha}")
        return self.case.get("merge_result", {"merged": True, "sha": "merge-1234567890"})


class _Events:
    def __init__(self, calls):
        self.calls = calls

    def emit(self, event, **values):
        self.calls.append(f"event:{event}:{values['outcome']}:{values.get('reason', '')}")


def _capture(case, monkeypatch):
    calls = []
    gateway = _Gateway(case, calls)
    hold = _Hold(calls, case.get("keep_error_at"))
    ticket = {"key": "TESTWP-1", "project": "TESTWP", "summary": "Sample ticket", "status": "Ready to merge"}
    ref = {"repository": "jooservices/test-aiflow-wp", "name": "test-aiflow-wp", "number": 7,
           "url": "https://github.com/jooservices/test-aiflow-wp/pull/7"}
    ctx = type("Context", (), {})()
    ctx.db, ctx.events = _Database(calls), _Events(calls)
    method = case.get("merge_method", "squash")
    ctx.repo = lambda: type("Repo", (), {"projects": {"TESTWP": type("Project", (), {"merge": {"method": method}})()}})()
    monkeypatch.setattr(merge_flow, "_gateway", lambda _ctx, _ref: gateway)
    monkeypatch.setattr(merge_flow, "_audit", lambda _ctx, target, action, _key, accepted, result, _payload=None:
                        calls.append(f"audit:{target}.{action}:{accepted}:{result}"))
    monkeypatch.setattr(merge_flow, "send_to_dev", lambda _ctx, _ticket, _ref, reason, **_kw:
                        calls.append(f"send:{reason}"))
    monkeypatch.setattr(merge_flow, "reconcile_one", lambda *_a, **_kw:
                        (calls.append("reconcile"), case.get("reconcile_result", True))[1])
    monkeypatch.setattr(merge_flow, "_comment", lambda _ctx, _key, text: calls.append(f"comment:{text}"))
    monkeypatch.setattr(merge_flow, "_note_run", lambda _ctx, _key, _project, outcome, summary, _findings="":
                        calls.append(f"note_run:{outcome}:{summary}"))
    monkeypatch.setattr(merge_flow, "record_merge", lambda _ctx, _key, _ref, sha, **kw:
                        calls.append(f"record_merge:{sha}:settled={kw['settled']}"))
    monkeypatch.setattr(merge_flow, "_move", lambda _ctx, _ticket, target, cause, detail, **_kw:
                        calls.append(f"move:{target}:{cause}:{detail}"))
    monkeypatch.setattr(merge_flow, "facts", lambda _row: {"approval_to_merge_s": case.get("seconds")})
    monkeypatch.setattr(merge_flow.log, "warning", lambda message, *args: calls.append("warning:" + message % args))
    queue_calls = [0]

    def merge_next(_db, _gateway, _repo, _number, _key, _sha, merge, **_kw):
        queue_calls[0] += 1
        if queue_calls[0] == 1:
            calls.append("queue:preflight")
            return case.get("preflight", {})
        calls.append("queue:merge")
        if "final_result" in case and not case.get("invoke_merge", True):
            return case["final_result"]
        return merge()

    monkeypatch.setattr(merge_flow.merge_queue, "merge_next", merge_next)
    monkeypatch.setattr(merge_flow.merge_queue, "finish", lambda _db, _repo, _number, merged:
                        calls.append(f"queue:finish:{merged}"))
    monkeypatch.setattr(merge_flow.merge_queue, "leave", lambda _db, _repo, _number:
                        calls.append("queue:leave"))
    try:
        outcome = merge_flow._merge_held(ctx, ticket, ref, hold)
    except Exception as error:  # the thrown type and text are part of the characterized result
        outcome = f"raised:{type(error).__name__}:{error}"
    return {"name": case["name"], "result": outcome, "calls": calls}


def _context(calls):
    ctx = type("Context", (), {})()
    ctx.db, ctx.events = _Database(calls), _Events(calls)
    return ctx


def _ticket_and_ref():
    ticket = {"key": "TESTWP-1", "project": "TESTWP", "summary": "Sample ticket", "status": "Ready to merge"}
    ref = {"repository": "jooservices/test-aiflow-wp", "name": "test-aiflow-wp", "number": 7,
           "url": "https://github.com/jooservices/test-aiflow-wp/pull/7"}
    return ticket, ref


def test_merge_held_characterization_outputs(monkeypatch):
    results = [_capture(case, monkeypatch) for case in CASES]
    actual = {result["name"]: (result["result"], "\n".join(result["calls"])) for result in results}
    assert len(CASES) == len(EXPECTED) >= 20
    assert actual == EXPECTED


def test_finish_if_github_settled(monkeypatch):
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()
    monkeypatch.setattr(merge_flow.merge_queue, "finish", lambda *_args: calls.append("finish"))
    monkeypatch.setattr(merge_flow, "reconcile_one", lambda *_args, **_kw: (calls.append("reconcile"), False)[1])

    assert merge_flow._finish_if_github_settled(ctx, ticket, ref, {"state": "open"}, _Hold(calls, None)) == (False, False)
    assert merge_flow._finish_if_github_settled(ctx, ticket, ref, {"merged": True}, _Hold(calls, None)) == (True, False)
    assert calls == ["finish", "reconcile"]


def test_check_queue_updated(monkeypatch):
    calls = []
    ctx, (_, ref) = _context(calls), _ticket_and_ref()
    monkeypatch.setattr(merge_flow, "_audit", lambda _ctx, _target, _action, _key, _accepted, message, _payload:
                        calls.append(f"audit:{message}"))

    assert merge_flow._check_queue_updated(ctx, "TESTWP-1", ref, {"updated": True, "message": "branch updated"})
    assert calls == ["audit:branch updated", "event:github.merge_queue:updated:branch updated",
                     "note:waiting: the branch was updated, the checks run again"]


def test_check_queue_conflict(monkeypatch):
    calls = []
    ticket, ref = _ticket_and_ref()
    monkeypatch.setattr(merge_flow, "send_to_dev", lambda _ctx, _ticket, _ref, reason, **_kw: calls.append(reason))

    assert merge_flow._check_queue_conflict(None, ticket, ref, {"conflict": True}, _Hold(calls, None))
    assert calls == ["The branch conflicts with the base branch."]


def test_check_queue_busy():
    calls = []
    ctx = _context(calls)

    assert merge_flow._check_queue_busy(ctx, "TESTWP-1", {"queued": True})
    assert calls == ["note:waiting: the repository merge queue is busy"]


def test_check_branch_update_failed():
    calls = []
    ctx = _context(calls)

    assert merge_flow._check_branch_update_failed(ctx, "TESTWP-1", {"update_failed": True, "message": "denied"})
    assert calls == ["note:waiting: denied"]


def test_handle_preflight_holds_keeps_its_reason_order(monkeypatch):
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()
    monkeypatch.setattr(merge_flow, "_audit", lambda *_args: calls.append("updated"))
    result = {"updated": True, "conflict": True, "queued": True, "update_failed": True}

    assert merge_flow._handle_preflight_holds(ctx, "TESTWP-1", ticket, ref, result, _Hold(calls, None))
    assert calls == ["updated", "event:github.merge_queue:updated:branch brought up to date with the base",
                     "note:waiting: the branch was updated, the checks run again"]


def test_handle_merge_queue_holds_keeps_its_reason_order():
    calls = []
    ctx = _context(calls)
    ticket, ref = _ticket_and_ref()
    result = {"queued": True, "updated": True, "conflict": True, "update_failed": True, "message": "busy"}

    assert merge_flow._handle_merge_queue_holds(ctx, "TESTWP-1", ticket, ref, result, _Hold(calls, None))
    assert calls == ["note:waiting: busy"]


def test_check_pr_conflict(monkeypatch):
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()
    monkeypatch.setattr(merge_flow.merge_queue, "leave", lambda *_args: calls.append("leave"))
    monkeypatch.setattr(merge_flow, "send_to_dev", lambda _ctx, _ticket, _ref, reason, **_kw: calls.append(reason))

    assert merge_flow._check_pr_conflict(ctx, ticket, ref, {"mergeable_state": "dirty"}, _Hold(calls, None))
    assert calls == ["leave", "The branch conflicts with the base branch."]


def test_check_failed_checks_caps_the_reason_to_eight(monkeypatch):
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()
    monkeypatch.setattr(merge_flow.merge_queue, "leave", lambda *_args: calls.append("leave"))
    monkeypatch.setattr(merge_flow, "send_to_dev", lambda _ctx, _ticket, _ref, reason, **_kw: calls.append(reason))
    checks = [{"name": f"Check-{n}", "url": str(n), "state": "failure"} for n in range(1, 10)]

    assert merge_flow._check_failed_checks(ctx, ticket, ref, checks, _Hold(calls, None))
    assert calls == ["leave", "Checks failed: " + ", ".join(f"Check-{n} ({n})" for n in range(1, 9))]


def test_check_pending_checks():
    calls = []
    ctx = _context(calls)

    assert merge_flow._check_pending_checks(ctx, "TESTWP-1", [{"state": "pending"}])
    assert calls == ["note:waiting: the checks are still running"]


def test_check_head_changed():
    calls = []
    ctx = _context(calls)

    assert merge_flow._check_head_changed(ctx, "TESTWP-1", {"message": "409: moved"})
    assert not merge_flow._check_head_changed(ctx, "TESTWP-1", {"message": " 409: moved"})
    assert calls == ["note:waiting: the branch changed after its checks were read; it is checked again"]


def test_record_github_refusal():
    calls = []
    ctx = _context(calls)

    merge_flow._record_github_refusal(ctx, "TESTWP-1", {"message": "checks pending"})
    assert calls == ["note:waiting: GitHub refused the merge (checks pending)"]


def test_handle_github_refusal_keeps_conflict_and_head_checks_ahead_of_generic(monkeypatch):
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()

    merge_flow._handle_github_refusal(ctx, "TESTWP-1", ticket, ref, {"message": "409: moved"}, _Hold(calls, None))
    assert calls == ["note:waiting: the branch changed after its checks were read; it is checked again"]


def test_complete_successful_merge(monkeypatch):
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()
    gateway = type("Gateway", (), {"pr": lambda *_args: {"merged_at": "2026-10-20T11:12:13Z"}})()
    monkeypatch.setattr(merge_flow, "record_merge", lambda _ctx, _key, _ref, sha, **_kw: calls.append(f"record:{sha}"))
    monkeypatch.setattr(merge_flow, "_move", lambda _ctx, _ticket, target, cause, detail, **_kw:
                        calls.append(f"move:{target}:{cause}:{detail}"))
    monkeypatch.setattr(merge_flow, "facts", lambda _row: {"approval_to_merge_s": 37})
    monkeypatch.setattr(merge_flow, "_comment", lambda _ctx, _key, text: calls.append(f"comment:{text.splitlines()[-1]}"))
    monkeypatch.setattr(merge_flow, "_note_run", lambda *_args: calls.append("note_run"))

    assert merge_flow._complete_successful_merge(ctx, "TESTWP-1", ticket, ref, gateway,
                                                  {"sha": "merge-sha"}, _Hold(calls, None),
                                                  merge_flow.datetime(2026, 10, 20, 11, 12, 13,
                                                                      tzinfo=merge_flow.timezone.utc))
    assert calls == ["keep", "record:merge-sha", "move:Done:flow merge:PR #7 merged as merge-sha",
                     "pr_state:merged", "ticket_row", "keep", "comment:Next: Ticket Done; 37 s between approval and merge.",
                     "note_run"]


def test_prepare_merge_preflight(monkeypatch):
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()
    gateway = type("Gateway", (), {"pr": lambda *_args: (calls.append("pr"), _open_pr())[1]})()
    monkeypatch.setattr(merge_flow.merge_queue, "merge_next", lambda *_args, **_kw: (calls.append("preflight"), {})[1])

    assert merge_flow._prepare_merge_preflight(ctx, "TESTWP-1", ticket, ref, gateway, _Hold(calls, None)) == (False, False)
    assert calls == ["keep", "pr", "preflight"]


def test_prepare_merge_head():
    calls = []
    ctx, (ticket, ref) = _context(calls), _ticket_and_ref()

    class Gateway:
        @staticmethod
        def pr(*_args):
            calls.append("pr")
            return _open_pr()

        @staticmethod
        def checks(_name, head):
            calls.append(f"checks:{head}")
            return [{"state": "success"}]

    assert merge_flow._prepare_merge_head(ctx, "TESTWP-1", ticket, ref, Gateway(), _Hold(calls, None)) == (
        False, False, "head-1")
    assert calls == ["keep", "pr", "keep", "checks:head-1"]

