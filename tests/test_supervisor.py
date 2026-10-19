import json
import os
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

    def get_many(self, names):
        return [self.data.get(n) for n in names]

    def scan(self, pattern):
        import fnmatch
        return [n for n in self.data if fnmatch.fnmatch(n, pattern)]


class FakeJira:
    def __init__(self, blockers=None):
        self._blockers = blockers or []
        self.comments = []
        self.moves = []
        self.labels = []

    def comment(self, key, body):
        self.comments.append((key, body))

    def search(self, jql, limit=50):
        return getattr(self, "found", [])

    def blockers(self, key):
        return self._blockers

    def transition(self, key, status):
        self.moves.append((key, status))
        return True

    def view(self, key):
        return {"key": key, "status": getattr(self, "status", "In Progress"), "agent": getattr(self, "agent", None)}

    def set_field(self, key, field, value):
        self.fields = getattr(self, "fields", {})
        self.fields[(key, field)] = value

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

    def test_human_stages_need_no_ai_and_are_polled(self):
        sup = make()
        for status in ("Human review", "Ready to merge"):
            issue = {"key": "JAIF-9", "status": status, "updated": stamp(10), "labels": []}
            self.assertTrue(sup.eligible(issue))
            sup.redis.data["gatepoll:JAIF-9"] = "1"
            self.assertFalse(sup.eligible(issue))
            sup.redis.data.pop("gatepoll:JAIF-9")

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

    def test_a_ticket_that_requires_a_human_stops_after_qa(self):
        self.assertEqual(decide(facts(require_human=True))[0], "human")
        self.assertEqual(decide(facts(require_human=True, qa_ok=False))[0], "qa")  # QA first
        self.assertEqual(decide(facts(require_human=True, ci="failed"))[0], "rework")  # CI and feedback first

    def test_after_the_approval_only_ci_and_the_base_matter(self):
        approved = facts(require_human=False, qa_ok=True, settled=True, auto_merge=True)
        self.assertEqual(decide(approved)[0], "merge")
        self.assertEqual(decide({**approved, "behind": True})[0], "update")
        self.assertEqual(decide({**approved, "ci": "pending"})[0], "wait")
        self.assertEqual(decide({**approved, "ci": "failed"})[0], "rework")

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

    def test_blocking_resets_the_counters_so_an_unblock_starts_fresh(self):
        sup = make()
        sup.redis.data.update({"attempts:K-1:developer": "3", "attempts:K-1:qa": "2", "cycles:K-1": "7", "dev:K-1": "Codex"})
        sup.block("K-1", "needs the owner", kind="Attempts")
        for name in ("attempts:K-1:developer", "attempts:K-1:qa", "cycles:K-1"):
            self.assertNotIn(name, sup.redis.data)
        self.assertIn("dev:K-1", sup.redis.data)  # who developed it is still known

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


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.original = agents.available
        agents.available = lambda order: ["Claude", "Codex"]

    def tearDown(self):
        agents.available = self.original

    def test_online_pool_adds_the_ais_of_other_machines(self):
        sup = make(AIFLOW_AGENT_ORDER="Claude,Codex,Grok,OpenCode")
        sup.redis.data["ais:mac"] = "Claude,Grok,OpenCode"
        self.assertEqual(sup.online_pool(), ["Claude", "Codex", "Grok", "OpenCode"])

    def test_reviewer_default_grok_is_taken_when_some_machine_has_it(self):
        sup = make(AIFLOW_AGENT_ORDER="Claude,Codex,Grok", AIFLOW_REVIEWER_DEFAULT="Grok")
        sup.redis.data["dev:K-1"] = "Codex"
        self.assertEqual(sup.pick_agent("reviewer", "K-1", "Codex"), "Claude")  # Grok is not on this machine
        self.assertEqual(sup.eligible_agents("reviewer", "K-1", "Codex", ["Claude", "Codex", "Grok"]), ["Grok"])

    def test_opencode_works_only_when_the_ticket_names_it(self):
        sup = make(AIFLOW_AGENT_ORDER="Claude,Codex,OpenCode")
        pool = ["Claude", "Codex", "OpenCode"]
        self.assertEqual(sup.eligible_agents("developer", "K-1", "OpenCode", pool), ["OpenCode"])
        self.assertEqual(sup.eligible_agents("developer", "K-1", None, pool), ["Claude", "Codex"])
        sup.redis.data["cooldown:Claude"] = sup.redis.data["cooldown:Codex"] = "1"
        self.assertEqual(sup.eligible_agents("developer", "K-1", None, pool), [])  # never falls back to OpenCode on its own
        sup.redis.data["dev:K-1"] = "Codex"
        self.assertEqual(sup.eligible_agents("reviewer", "K-1", "Codex", pool), [])

    def test_nobody_waits_when_another_machine_can_take_the_step(self):
        sup = make(AIFLOW_AGENT_ORDER="Claude,Codex,Grok", AIFLOW_QUOTA_BLOCK_MINUTES="0")
        sup.redis.data["ais:mac"] = "Claude,Grok"
        sup.redis.data["dev:K-1"] = "Codex"
        sup.redis.data["waiting:K-1:reviewer"] = "1"
        sup.unserved("K-1", "reviewer", "Codex")
        self.assertEqual(sup.jira.moves, [])

    def test_blocks_with_reason_quota_when_every_candidate_is_cooling(self):
        sup = make(AIFLOW_QUOTA_BLOCK_MINUTES="0")
        sup.redis.data.update({"dev:K-1": "Codex", "cooldown:Claude": "1", "waiting:K-1:reviewer": "1"})
        sup.unserved("K-1", "reviewer", "Codex")
        self.assertEqual(sup.jira.moves, [("K-1", "Blocked")])
        self.assertTrue(sup.jira.fields[("K-1", sup.cfg.blocked_reason_field)].startswith("Quota:"))
        self.assertEqual(json.loads(sup.redis.data["blocked:K-1"])["kind"], "Quota")

    def test_blocks_with_reason_offline_when_no_machine_has_the_ai(self):
        sup = make(AIFLOW_QUOTA_BLOCK_MINUTES="0", AIFLOW_AGENT_ORDER="Claude,Codex,OpenCode")
        sup.redis.data["waiting:K-1:developer"] = "1"
        sup.unserved("K-1", "developer", "OpenCode")
        self.assertTrue(sup.jira.fields[("K-1", sup.cfg.blocked_reason_field)].startswith("Offline:"))

    def test_waits_before_blocking(self):
        sup = make()
        sup.redis.data.update({"dev:K-1": "Codex", "cooldown:Claude": "1"})
        sup.unserved("K-1", "reviewer", "Codex")
        sup.unserved("K-1", "reviewer", "Codex")
        self.assertEqual(sup.jira.moves, [])

    def test_recheck_returns_a_quota_ticket_when_an_ai_is_back(self):
        sup = make()
        sup.jira.found = [{"key": "K-1", "labels": []}]
        sup.jira.status = "Blocked"
        sup.redis.data.update({"blocked:K-1": json.dumps({"status": "In Review", "kind": "Quota"}), "dev:K-1": "Codex"})
        sup.recheck_blocked()
        self.assertEqual(sup.jira.moves, [("K-1", "In Review")])
        self.assertIsNone(sup.jira.fields[("K-1", sup.cfg.blocked_reason_field)])
        self.assertNotIn("blocked:K-1", sup.redis.data)

    def test_recheck_keeps_a_ticket_blocked_while_every_ai_is_cooling(self):
        sup = make()
        sup.jira.found = [{"key": "K-1", "labels": []}]
        sup.redis.data.update({"blocked:K-1": json.dumps({"status": "In Review", "kind": "Quota"}), "dev:K-1": "Codex", "cooldown:Claude": "1"})
        sup.recheck_blocked()
        self.assertEqual(sup.jira.moves, [])

    def test_recheck_ignores_blocks_that_need_a_human(self):
        sup = make()
        sup.jira.found = [{"key": "K-1", "labels": []}]
        sup.redis.data["blocked:K-1"] = json.dumps({"status": "In Progress", "kind": "Unclear"})
        sup.recheck_blocked()
        self.assertEqual(sup.jira.moves, [])


class ModelFieldTests(unittest.TestCase):
    def sup(self, value, **env):
        sup = make(**env)
        sup.jira.read_field = lambda key, field: value
        return sup

    def test_spec_is_split_at_the_first_colon(self):
        self.assertEqual(self.sup({"value": "Codex:gpt-6-luna"}).ticket_spec("K-1"), ("Codex", "gpt-6-luna"))
        self.assertEqual(self.sup({"value": "OpenCode:opencode/deepseek-v4.1-flash-free"}).ticket_spec("K-1"),
                         ("OpenCode", "opencode/deepseek-v4.1-flash-free"))

    def test_empty_or_unknown_values_mean_the_default(self):
        for value in (None, {"value": ""}, {"value": "Gemini:pro"}, {"value": "Claude"}, {"value": "Claude:"}):
            self.assertEqual(self.sup(value).ticket_spec("K-1"), ("", ""), value)

    def test_the_ticket_model_wins_for_the_ai_that_was_chosen(self):
        sup = make(AIFLOW_DEVELOPER_DEFAULT="Codex:gpt-6-luna:xhigh")
        self.assertEqual(sup.model_for("developer", "Codex", "K-1"), ("gpt-6-luna", "xhigh"))
        sup.redis.data["devmodel:K-1"] = "Codex:gpt-5.6-terra"
        self.assertEqual(sup.model_for("developer", "Codex", "K-1"), ("gpt-5.6-terra", "xhigh"))
        self.assertEqual(sup.model_for("developer", "Claude", "K-1"), ("", ""))  # a takeover by another AI uses its default

    def test_other_roles_ignore_the_ticket_model(self):
        sup = make(AIFLOW_REVIEWER_DEFAULT="Grok:grok-4.7")
        sup.redis.data["devmodel:K-1"] = "Grok:grok-4.5"
        self.assertEqual(sup.model_for("reviewer", "Grok", "K-1"), ("grok-4.7", ""))


class RunEnvTests(unittest.TestCase):
    def test_pwd_is_the_worktree_for_every_ai(self):
        for agent in ("Claude", "Codex", "Grok", "OpenCode"):
            self.assertEqual(agents.run_env(agent, Path("/w/JAIF-1-x"), Path("/r"))["PWD"], "/w/JAIF-1-x")

    def test_opencode_may_use_the_adapter_and_comment_files_outside_the_worktree(self):
        config = json.loads(agents.run_env("OpenCode", Path("/w/JAIF-1-x"), Path("/r"))["OPENCODE_CONFIG_CONTENT"])
        allowed = config["permission"]["external_directory"]
        self.assertEqual(allowed["/r/scripts/*"], "allow")
        self.assertEqual(allowed["/r/.tmp/*"], "allow")
        self.assertNotIn("/r/*", allowed)
        original = os.environ.get("OPENCODE_CONFIG_CONTENT")
        os.environ["OPENCODE_CONFIG_CONTENT"] = "{}"  # an OpenCode parent process (a test run by OpenCode) must not leak it
        try:
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", agents.run_env("Codex", Path("/w"), Path("/r")))
        finally:
            os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
            if original is not None:
                os.environ["OPENCODE_CONFIG_CONTENT"] = original


class AgentCommandTests(unittest.TestCase):
    def test_grok_uses_a_prompt_file_and_the_plain_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, stdin = agents.build_command("Grok", Path(tmp), Path(tmp) / "r.result", "the prompt", Path(tmp))
            self.assertIsNone(stdin)
            self.assertIn("--prompt-file", argv)
            self.assertEqual(Path(argv[argv.index("--prompt-file") + 1]).read_text(), "the prompt")
            self.assertEqual(argv[argv.index("--output-format") + 1], "plain")

    def test_opencode_gets_the_model(self):
        argv, _ = agents.build_command("OpenCode", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"), "openai/gpt-5.4-mini")
        self.assertEqual(argv[:4], ["opencode", "run", "-m", "openai/gpt-5.4-mini"])

    def test_opencode_model_default_comes_from_the_settings(self):
        self.assertEqual(make(AIFLOW_OPENCODE_MODEL="opencode/big-pickle").model_for("developer", "OpenCode"), ("opencode/big-pickle", ""))

    def test_unusable_account_messages_count_as_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            for text in ("API error (status 402 Payment Required): Grok Build usage balance exhausted",
                         "Error: Not signed in. To authenticate", "This API key has expired.", "rate limit reached"):
                log.write_text(text)
                self.assertTrue(agents.looks_like_quota(log, ""), text)
            log.write_text("reviewed line 402 of the file; 4020 bytes")
            self.assertFalse(agents.looks_like_quota(log, "all good"))

    def test_words_in_file_contents_and_branch_names_are_not_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            log.write_text("7c91f22 Merge pull request #5 from jooservices/JAIF-26-roles-quota\nthe quota of the day\nrate limit table")
            self.assertFalse(agents.looks_like_quota(log, ""))

    def test_a_long_log_counts_only_when_the_process_failed_and_only_at_its_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            log.write_text("usage limit reached (quoted from a README)\n" + "work\n" * 3000)
            self.assertFalse(agents.looks_like_quota(log, "", 0))
            self.assertFalse(agents.looks_like_quota(log, "", 1))  # the phrase is far from the end
            log.write_text("work\n" * 3000 + "Error: usage limit reached, resets at 5pm")
            self.assertFalse(agents.looks_like_quota(log, "", 0))
            self.assertTrue(agents.looks_like_quota(log, "", 1))


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
