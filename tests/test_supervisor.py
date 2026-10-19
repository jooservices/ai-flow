import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiflow import agents, github
from aiflow.config import load
from aiflow.gitops import GitError
from aiflow.supervisor import Supervisor, decide, ticket_field


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value, ttl):
        self.data[name] = value

    def acquire(self, name, owner, ttl):
        if name in self.data:
            return False
        self.data[name] = owner
        return True

    def release(self, name, owner):
        return self.data.pop(name, None) is not None

    def renew(self, name, owner, ttl):
        return self.data.get(name) == owner

    def incr(self, name, ttl):
        self.data[name] = str(int(self.data.get(name, 0)) + 1)
        return int(self.data[name])

    def delete(self, name):
        self.data.pop(name, None)


class FakeJira:
    def __init__(self, blockers=None):
        self._blockers = blockers or []
        self.comments = []
        self.moves = []
        self.labels = []

    def comment(self, key, body):
        self.comments.append((key, body))

    def search(self, jql, limit=50):
        return []

    def blockers(self, key):
        return self._blockers

    def transition(self, key, status):
        self.moves.append((key, status))
        return True

    def add_label(self, key, label):
        self.labels.append((key, label))

    def remove_label(self, key, label):
        pass


def stamp(seconds_ago):
    value = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return value.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def make(blockers=None, redis=None, **env):
    cfg = load({"AIFLOW_ROOT": tempfile.gettempdir(), "AIFLOW_MACHINE": "test", **env})
    return Supervisor(cfg, FakeJira(blockers), redis or FakeRedis())


class VerdictTests(unittest.TestCase):
    def test_last_verdict_wins(self):
        text = "RESULT: DONE\nmore work\nRESULT: BLOCKED: need a decision"
        self.assertEqual(agents.verdict(text), ("BLOCKED", "need a decision"))

    def test_fail_reason(self):
        self.assertEqual(agents.verdict("notes\nVERDICT: FAIL: tests missing"), ("FAIL", "tests missing"))

    def test_no_verdict(self):
        self.assertIsNone(agents.verdict("finished everything"))

    def test_quota_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            log.write_text("error: Claude usage limit reached, resets at 5pm")
            self.assertTrue(agents.looks_like_quota(log, ""))
            log.write_text("all fine")
            self.assertFalse(agents.looks_like_quota(log, "done"))


class AgentChoiceTests(unittest.TestCase):
    def test_other_than_developer(self):
        self.assertEqual(agents.choose_other("Codex", ["Claude", "Codex"]), "Claude")

    def test_prefers_not_reviewer(self):
        self.assertEqual(agents.choose_other("Codex", ["Claude", "OpenCode"], avoid=("Claude",)), "OpenCode")

    def test_falls_back_when_only_reviewer_left(self):
        self.assertEqual(agents.choose_other("Codex", ["Claude", "Codex"], avoid=("Claude",)), "Claude")

    def test_none_when_only_developer(self):
        self.assertIsNone(agents.choose_other("Codex", ["Codex"]))


class TicketTests(unittest.TestCase):
    def test_fields_from_flattened_description(self):
        text = "Goal do a thing. Repo: ai-flow Base: develop Acceptance"
        self.assertEqual(ticket_field(text, "Repo", "x"), "ai-flow")
        self.assertEqual(ticket_field(text, "Base", "x"), "develop")
        self.assertEqual(ticket_field("nothing", "Repo", "dflt"), "dflt")


class EligibilityTests(unittest.TestCase):
    def issue(self, status="To Do", age=1000, labels=None):
        return {"key": "JAIF-9", "status": status, "updated": stamp(age), "labels": labels or []}

    def test_to_do_waits_for_blockers(self):
        self.assertFalse(make([{"key": "JAIF-1", "status": "In Progress"}]).eligible(self.issue()))
        self.assertTrue(make([{"key": "JAIF-1", "status": "Done"}]).eligible(self.issue()))

    def test_lease_blocks_pickup(self):
        redis = FakeRedis()
        redis.acquire("lease:JAIF-9", "other", 60)
        self.assertFalse(make(redis=redis).eligible(self.issue()))

    def test_in_progress_needs_orphan_grace(self):
        self.assertFalse(make().eligible(self.issue("In Progress", age=10)))
        self.assertTrue(make().eligible(self.issue("In Progress", age=1000)))

    def test_stale_search_result_for_a_finished_ticket(self):
        self.assertFalse(make().eligible(self.issue("Done")))

    def test_hold_label(self):
        self.assertFalse(make().eligible(self.issue(labels=["aiflow-hold"])))


class GateEligibilityTests(unittest.TestCase):
    def test_in_qa_waits_between_polls(self):
        sup = make()
        issue = {"key": "JAIF-9", "status": "In QA", "updated": stamp(10), "labels": []}
        self.assertTrue(sup.eligible(issue))
        sup.redis.data["gatepoll:JAIF-9"] = "1"
        self.assertFalse(sup.eligible(issue))

    def test_rework_tickets_come_first(self):
        sup = make()
        sup.jira.search = lambda jql, limit=50: [{"key": "A-1", "labels": []}, {"key": "A-2", "labels": ["rework"]}, {"key": "A-3", "labels": []}]
        self.assertEqual([i["key"] for i in sup.candidates()], ["A-2", "A-1", "A-3"])


def facts(**over):
    base = dict(state="OPEN", behind=False, conflicting=False, ci="passed", failing=[], ci_required=False, ci_wait_minutes=30,
                ci_waited_minutes=0, settled=True, stuck=[], feedback=[], qa_ok=True, auto_merge=True)
    base.update(over)
    return base


class GateDecisionTests(unittest.TestCase):
    def test_all_green_merges(self):
        self.assertEqual(decide(facts())[0], "merge")

    def test_auto_merge_off_waits_for_the_owner(self):
        self.assertEqual(decide(facts(auto_merge=False))[0], "awaiting")

    def test_merged_and_closed(self):
        self.assertEqual(decide(facts(state="MERGED"))[0], "done")
        self.assertEqual(decide(facts(state="CLOSED"))[0], "closed")

    def test_behind_updates_or_reworks_on_conflict(self):
        self.assertEqual(decide(facts(behind=True))[0], "update")
        self.assertEqual(decide(facts(behind=True, conflicting=True))[0], "rework")

    def test_ci_states(self):
        self.assertEqual(decide(facts(ci="failed"))[0], "rework")
        self.assertEqual(decide(facts(ci="pending"))[0], "wait")
        self.assertEqual(decide(facts(ci="none"))[0], "merge")
        self.assertEqual(decide(facts(ci="none", ci_required=True))[0], "wait")
        self.assertEqual(decide(facts(ci="none", ci_required=True, ci_waited_minutes=31))[0], "block")

    def test_pending_ci_waits_below_timeout(self):
        self.assertEqual(decide(facts(ci="pending", ci_waited_minutes=29)), ("wait", "CI is running"))

    def test_pending_ci_blocks_at_timeout(self):
        self.assertEqual(decide(facts(ci="pending", ci_waited_minutes=30)),
                         ("block", "CI is still running after 30 minutes"))

    def test_feedback_blocks_qa_and_merge(self):
        self.assertEqual(decide(facts(feedback=[{"id": "x"}], qa_ok=False))[0], "rework")
        self.assertEqual(decide(facts(settled=False, feedback=[{"id": "x"}]))[0], "wait")

    def test_human_review_still_requesting_changes_blocks(self):
        self.assertEqual(decide(facts(stuck=["jooservices"]))[0], "block")

    def test_qa_runs_on_a_new_head_only(self):
        self.assertEqual(decide(facts(qa_ok=False))[0], "qa")

    def test_base_update_beats_a_stale_qa(self):
        self.assertEqual(decide(facts(behind=True, qa_ok=True))[0], "update")


class GitHubParsingTests(unittest.TestCase):
    def test_titles(self):
        self.assertTrue(github.valid_title("feat(warashi): Return the related scene (JOOCX-61)"))
        self.assertTrue(github.valid_title("chore: Document the gate (JAIF-20)"))
        for bad in ("JAIF-20: add a thing", "feat: lower case start", "fix: Ends with a dot.", "fixed: Wrong type here", "feat: Hi"):
            self.assertFalse(github.valid_title(bad), bad)

    def test_fallback_title_is_valid(self):
        for kind in ("Task", "Story", "Bug"):
            self.assertTrue(github.valid_title(github.fallback_title("JAIF-20", "add a thing to the flow.", kind)))
        self.assertTrue(github.valid_title(github.fallback_title("JAIF-20", "x" * 300, "Task")))

    def test_key_is_appended_once(self):
        self.assertEqual(github.with_key("feat: Add it", "JAIF-1"), "feat: Add it (JAIF-1)")
        self.assertEqual(github.with_key("feat: Add it (JAIF-1)", "JAIF-1"), "feat: Add it (JAIF-1)")

    def test_checks_state(self):
        run = lambda status, conclusion="": {"__typename": "CheckRun", "name": "t", "status": status, "conclusion": conclusion, "detailsUrl": "u"}
        self.assertEqual(github.checks_state([])[0], "none")
        self.assertEqual(github.checks_state([run("COMPLETED", "SUCCESS"), run("COMPLETED", "SKIPPED")])[0], "passed")
        self.assertEqual(github.checks_state([run("COMPLETED", "SUCCESS"), run("IN_PROGRESS")])[0], "pending")
        state, failing = github.checks_state([run("IN_PROGRESS"), run("COMPLETED", "FAILURE")])
        self.assertEqual((state, len(failing)), ("failed", 1))
        self.assertEqual(github.checks_state([{"__typename": "StatusContext", "context": "ci", "state": "PENDING"}])[0], "pending")

    def test_feedback_split(self):
        bot, human = {"login": "coderabbitai", "__typename": "Bot"}, {"login": "jooservices", "__typename": "User"}
        data = {
            "reviewThreads": {"nodes": [
                {"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 3, "comments": {"nodes": [{"id": "c1", "body": "bad name", "author": bot}]}},
                {"id": "T2", "isResolved": True, "isOutdated": False, "path": "b.py", "line": 1, "comments": {"nodes": [{"id": "c2", "body": "ok", "author": human}]}},
            ]},
            "comments": {"nodes": [
                {"id": "i1", "body": "walkthrough", "author": bot, "createdAt": "", "url": ""},
                {"id": "i2", "body": "please also update docs", "author": human, "createdAt": "", "url": ""},
                {"id": "i3", "body": f"Fixed: done {github.MARKER}", "author": human, "createdAt": "", "url": ""},
            ]},
            "reviews": {"nodes": [{"id": "r1", "state": "CHANGES_REQUESTED", "body": "no", "author": human}]},
        }
        found = github.parse_feedback(data, set())
        self.assertEqual([i["id"] for i in found["items"]], ["thread:T1", "comment:i2", "review:r1"])
        self.assertTrue(found["items"][0]["bot"])
        self.assertEqual(found["stuck"], [])
        data["comments"]["nodes"].append({"id": "i4", "body": "Codecov Report", "author": {"login": "codecov-commenter", "__typename": "User"}, "createdAt": "", "url": ""})
        self.assertEqual([i["id"] for i in github.parse_feedback(data, set(), ("codecov-commenter",))["items"]], ["thread:T1", "comment:i2", "review:r1"])
        again = github.parse_feedback(data, {"comment:i2", "review:r1", "comment:i4"})
        self.assertEqual([i["id"] for i in again["items"]], ["thread:T1"])
        self.assertEqual(again["stuck"], ["jooservices"])


class RoleDefaultTests(unittest.TestCase):
    def test_spec_parts(self):
        self.assertEqual(agents.parse_spec("Claude:sonnet:high"), ("Claude", "sonnet", "high"))
        self.assertEqual(agents.parse_spec(""), ("", "", ""))

    def test_model_only_for_the_named_agent(self):
        sup = make(AIFLOW_REVIEWER_DEFAULT="Claude:sonnet")
        self.assertEqual(sup.model_for("reviewer", "Claude"), ("sonnet", ""))
        self.assertEqual(sup.model_for("reviewer", "Codex"), ("", ""))

    def test_reviewer_default_is_used_unless_it_is_the_developer(self):
        sup = make(AIFLOW_REVIEWER_DEFAULT="Claude:sonnet")
        original = agents.available
        agents.available = lambda order: ["Claude", "Codex"]
        try:
            sup.redis.data["dev:JAIF-9"] = "Codex"
            self.assertEqual(sup.pick_agent("reviewer", "JAIF-9", "Codex"), "Claude")
            sup.redis.data["dev:JAIF-9"] = "Claude"
            self.assertEqual(sup.pick_agent("reviewer", "JAIF-9", "Claude"), "Codex")
            self.assertEqual(sup.pick_agent("developer", "JAIF-9", None), "Claude")
        finally:
            agents.available = original

    def test_developer_default_applies_when_the_field_is_empty(self):
        sup = make(AIFLOW_DEVELOPER_DEFAULT="Codex")
        original = agents.available
        agents.available = lambda order: ["Claude", "Codex"]
        try:
            self.assertEqual(sup.pick_agent("developer", "JAIF-9", None), "Codex")
            self.assertEqual(sup.pick_agent("developer", "JAIF-9", "Claude"), "Claude")
        finally:
            agents.available = original


class FakeRepo:
    name = "ai-flow"


class RecordingCleanupRepo(FakeRepo):
    def __init__(self, refresh_error=None, cleanup_error=None):
        self.calls = []
        self.refresh_error = refresh_error
        self.cleanup_error = cleanup_error

    def refresh_base(self, base):
        self.calls.append(("refresh_base", base))
        if self.refresh_error:
            raise self.refresh_error

    def cleanup(self, key):
        self.calls.append(("cleanup", key))
        if self.cleanup_error:
            raise self.cleanup_error


class FinishTests(unittest.TestCase):
    def test_refreshes_base_before_cleanup(self):
        sup = make()
        repo = RecordingCleanupRepo()

        sup.finish("JAIF-9", repo, "develop", {}, "Merged pull request.")

        self.assertEqual(repo.calls, [("refresh_base", "develop"), ("cleanup", "JAIF-9")])
        self.assertIn("Worktree and branch removed.", sup.jira.comments[-1][1])

    def test_refresh_error_is_reported_and_finish_continues(self):
        sup = make()
        repo = RecordingCleanupRepo(refresh_error=GitError("fetch failed"))

        sup.finish("JAIF-9", repo, "develop", {}, "Merged pull request.")

        self.assertIn(
            "Cleanup failed (fetch failed); remove the worktree by hand.",
            sup.jira.comments[-1][1],
        )
        self.assertEqual(sup.jira.moves, [("JAIF-9", "Done")])

    def test_cleanup_error_is_reported_in_the_ticket_comment(self):
        sup = make()
        repo = RecordingCleanupRepo(cleanup_error=GitError("not fully merged"))

        sup.finish("JAIF-9", repo, "develop", {}, "Merged pull request.")

        self.assertIn(
            "Cleanup failed (not fully merged); remove the worktree by hand.",
            sup.jira.comments[-1][1],
        )
        self.assertEqual(repo.calls, [("refresh_base", "develop"), ("cleanup", "JAIF-9")])
        self.assertEqual(sup.jira.moves, [("JAIF-9", "Done")])


class SendBackTests(unittest.TestCase):
    ITEM = {"id": "thread:T1", "kind": "thread", "bot": True, "who": "coderabbitai", "where": "a.py:3", "text": "bad name"}

    def test_goes_to_to_do_with_rework_label_and_reason(self):
        sup = make()
        sup.redis.data["qaslot:ai-flow"] = "JAIF-9"
        sup.send_back("JAIF-9", FakeRepo(), "CI failed", "Failing checks: t", [self.ITEM])
        self.assertEqual(sup.jira.moves, [("JAIF-9", "To Do")])
        self.assertEqual(sup.jira.labels, [("JAIF-9", "rework")])
        self.assertIn("CI failed", sup.redis.data["rework:JAIF-9"])
        self.assertIn("thread:T1", sup.redis.data["feedback:JAIF-9"])
        self.assertNotIn("qaslot:ai-flow", sup.redis.data)  # the repository slot is free for the next pull request

    def test_too_many_rounds_block_the_ticket(self):
        sup = make(AIFLOW_MAX_CYCLES="2")
        for _ in range(3):
            sup.send_back("JAIF-9", FakeRepo(), "again", "x", [])
        self.assertEqual(sup.jira.moves[-1], ("JAIF-9", "Blocked"))

    def test_review_items_are_remembered_as_sent(self):
        sup = make()
        review = {"id": "review:R1", "kind": "review", "bot": False, "who": "jooservices", "where": "review", "text": "no"}
        sup.send_back("JAIF-9", FakeRepo(), "feedback", "x", [review, self.ITEM])
        self.assertEqual(json.loads(sup.redis.data["seen:JAIF-9"]), ["review:R1"])


class RepliesTests(unittest.TestCase):
    def test_every_item_needs_a_valid_answer(self):
        sup = make()
        sup.redis.data["feedback:JAIF-9"] = json.dumps([SendBackTests.ITEM])
        path = sup.replies_file("JAIF-9")
        path.unlink(missing_ok=True)
        self.assertIn("answers in", sup.read_replies("JAIF-9"))
        path.write_text(json.dumps([{"id": "thread:T1", "action": "maybe", "reply": "x"}]))
        self.assertIn("no valid answer", sup.read_replies("JAIF-9"))
        path.write_text(json.dumps([{"id": "thread:T1", "action": "rejected", "reply": "the name is required by the API"}]))
        answers = sup.read_replies("JAIF-9")
        self.assertEqual(answers[0][1]["action"], "rejected")
        path.unlink()

    def test_no_feedback_means_nothing_to_answer(self):
        self.assertEqual(make().read_replies("JAIF-9"), [])


class WaitingNoteTests(unittest.TestCase):
    def test_note_is_written_once_after_the_wait(self):
        sup = make()
        sup.note_waiting("JAIF-9", "reviewer")
        self.assertEqual(sup.jira.comments, [])
        sup.redis.data["waiting:JAIF-9:reviewer"] = "1"  # waiting since 1970
        sup.note_waiting("JAIF-9", "reviewer")
        sup.note_waiting("JAIF-9", "reviewer")
        self.assertEqual(len(sup.jira.comments), 1)


class HealthTests(unittest.TestCase):
    def test_missing_heartbeat_is_a_problem(self):
        import time
        from aiflow import health
        sup = make()
        cfg = load({"AIFLOW_ROOT": tempfile.gettempdir(), "AIFLOW_EXPECTED_SLOTS": "wsl:1,wsl:2"})
        sup.redis.data["hb:wsl:1"] = str(int(time.time()))
        kinds = [kind for kind, _ in health.problems(cfg, sup.redis, sup.jira)]
        self.assertEqual(kinds, ["slot-wsl:2"])


class LeaseRaceTests(unittest.TestCase):
    def test_second_supervisor_cannot_take_the_lease(self):
        redis = FakeRedis()
        self.assertTrue(redis.acquire("lease:JAIF-9", "wsl:1", 60))
        self.assertFalse(redis.acquire("lease:JAIF-9", "mac:1", 60))


if __name__ == "__main__":
    unittest.main()
